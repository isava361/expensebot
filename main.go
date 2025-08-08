package main

// Telegram expense-splitting bot using gotgbot + SQLite (pure Go driver)
// Features (MVP):
// 1) Trip groups with invite links
// 2) Add expense via inline buttons: choose payer, participants, split equally or custom
// 3) Cross-group netting summary
// 4) Delete expense
// 5) Confirm settlement (mark a debt as paid)
// 6) Concurrency-safe state handling
// 7) Uses SQLite
//
// Env:
//   BOT_TOKEN=<telegram bot token>
//   DB_PATH=./data.db
//
// NOTE: This is an MVP: focus on clear flows and correctness.
// Production hardening left as an exercise: error handling polish, paging, i18n, tests, etc.

import (
	"context"
	"crypto/rand"
	"database/sql"
	"encoding/base64"
	"errors"
	"fmt"
	"log"
	"os"
	"strconv"
	"strings"
	"sync"
	"time"

	tgb "github.com/PaulSonOfLars/gotgbot/v2"
	"github.com/PaulSonOfLars/gotgbot/v2/ext"
	"github.com/PaulSonOfLars/gotgbot/v2/ext/handlers"
	"github.com/PaulSonOfLars/gotgbot/v2/ext/handlers/filters/callbackquery"
	"github.com/PaulSonOfLars/gotgbot/v2/ext/handlers/filters/message"
	_ "modernc.org/sqlite"
)

// ----- DB -----

func mustOpenDB() *sql.DB {
	path := getenv("DB_PATH", "./data.db")
	db, err := sql.Open("sqlite", path)
	if err != nil {
		log.Fatalf("open db: %v", err)
	}
	db.SetMaxOpenConns(1) // SQLite + WAL; limit connections
	if err := initSchema(db); err != nil {
		log.Fatalf("schema: %v", err)
	}
	return db
}

func initSchema(db *sql.DB) error {
	schema := `
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS users(
    tg_id INTEGER PRIMARY KEY,
    name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS groups(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    owner_tg_id INTEGER NOT NULL,
    invite_code TEXT NOT NULL UNIQUE,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS group_members(
    group_id INTEGER NOT NULL,
    tg_id INTEGER NOT NULL,
    role TEXT NOT NULL DEFAULT 'member',
    PRIMARY KEY(group_id, tg_id),
    FOREIGN KEY(group_id) REFERENCES groups(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS expenses(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER NOT NULL,
    payer_tg_id INTEGER NOT NULL,
    description TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    deleted INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY(group_id) REFERENCES groups(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS expense_participants(
    expense_id INTEGER NOT NULL,
    participant_tg_id INTEGER NOT NULL,
    share_cents INTEGER NOT NULL,
    PRIMARY KEY(expense_id, participant_tg_id),
    FOREIGN KEY(expense_id) REFERENCES expenses(id) ON DELETE CASCADE
);

-- Settlements represent an explicit payment made to settle debts (optional, for confirmations).
CREATE TABLE IF NOT EXISTS settlements(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER, -- nullable means cross-group settlement (aggregate)
    from_tg_id INTEGER NOT NULL,
    to_tg_id INTEGER NOT NULL,
    amount_cents INTEGER NOT NULL,
    confirmed_by_to INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);
`
	_, err := db.Exec(schema)
	return err
}

// ----- Utilities -----

func getenv(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}

func nowUnix() int64 { return time.Now().Unix() }

func centsFromStr(s string) (int64, error) {
	// Accept "123.45" or "123"
	s = strings.ReplaceAll(strings.TrimSpace(s), ",", ".")
	if s == "" {
		return 0, errors.New("empty amount")
	}
	if strings.Contains(s, ".") {
		parts := strings.SplitN(s, ".", 2)
		intPart := parts[0]
		frac := parts[1]
		if len(frac) == 1 {
			frac += "0"
		}
		if len(frac) > 2 {
			frac = frac[:2]
		}
		i, err := strconv.ParseInt(intPart+frac, 10, 64)
		if err != nil {
			return 0, err
		}
		return i, nil
	}
	i, err := strconv.ParseInt(s, 10, 64)
	if err != nil {
		return 0, err
	}
	return i * 100, nil
}

func formatCents(c int64) string {
	sign := ""
	if c < 0 {
		sign = "-"
		c = -c
	}
	return fmt.Sprintf("%s%d.%02d", sign, c/100, c%100)
}

func randCode(n int) (string, error) {
	b := make([]byte, n)
	if _, err := rand.Read(b); err != nil {
		return "", err
	}
	return base64.RawURLEncoding.EncodeToString(b), nil
}

// ----- Bot state for flows -----

type addExpenseState struct {
	GroupID      int64
	AmountCents  int64
	Description  string
	Payer        int64
	Participants map[int64]bool // set
	SplitMode    string          // "equal" or "custom"
	CustomLeft   []int64         // order for custom input
	CustomShares map[int64]int64 // participant -> cents
	Step         string          // which step we're on
}

type stateStore struct {
	mu    sync.RWMutex
	addEx map[int64]*addExpenseState // by user tg id
}

func newStateStore() *stateStore {
	return &stateStore{addEx: map[int64]*addExpenseState{}}
}

func (s *stateStore) Get(uid int64) (*addExpenseState, bool) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	st, ok := s.addEx[uid]
	return st, ok
}

func (s *stateStore) Set(uid int64, st *addExpenseState) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.addEx[uid] = st
}

func (s *stateStore) Del(uid int64) {
	s.mu.Lock()
	defer s.mu.Unlock()
	delete(s.addEx, uid)
}

// ----- Repo helpers -----

type repo struct{ db *sql.DB }

func (r *repo) upsertUser(tgID int64, name string) error {
	_, err := r.db.Exec(`INSERT INTO users(tg_id,name) VALUES(?,?)
                         ON CONFLICT(tg_id) DO UPDATE SET name=excluded.name`, tgID, name)
	return err
}

func (r *repo) createGroup(title string, owner int64) (int64, string, error) {
	code, err := randCode(6)
	if err != nil {
		return 0, "", err
	}
	res, err := r.db.Exec(`INSERT INTO groups(title,owner_tg_id,invite_code,created_at) VALUES(?,?,?,?)`,
		title, owner, code, nowUnix())
	if err != nil {
		return 0, "", err
	}
	gid, _ := res.LastInsertId()
	if _, err := r.db.Exec(`INSERT INTO group_members(group_id,tg_id,role) VALUES(?,?,?)`, gid, owner, "owner"); err != nil {
		return 0, "", err
	}
	return gid, code, nil
}

func (r *repo) joinByCode(code string, uid int64) (int64, string, error) {
	var gid int64
	var title string
	err := r.db.QueryRow(`SELECT id,title FROM groups WHERE invite_code=?`, code).Scan(&gid, &title)
	if err != nil {
		return 0, "", err
	}
	_, err = r.db.Exec(`INSERT INTO group_members(group_id,tg_id,role) VALUES(?,?,?)
                        ON CONFLICT(group_id,tg_id) DO NOTHING`, gid, uid, "member")
	return gid, title, err
}

func (r *repo) listUserGroups(uid int64) ([]struct{ ID int64; Title string }, error) {
	rows, err := r.db.Query(`SELECT g.id,g.title FROM groups g
		JOIN group_members m ON g.id=m.group_id
		WHERE m.tg_id=? ORDER BY g.created_at DESC`, uid)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var res []struct{ ID int64; Title string }
	for rows.Next() {
		var x struct{ ID int64; Title string }
		if err := rows.Scan(&x.ID, &x.Title); err != nil {
			return nil, err
		}
		res = append(res, x)
	}
	return res, nil
}

func (r *repo) listMembers(groupID int64) ([]struct{ ID int64; Name string }, error) {
	rows, err := r.db.Query(`SELECT u.tg_id,u.name FROM group_members m
		JOIN users u ON u.tg_id=m.tg_id WHERE m.group_id=? ORDER BY u.name`, groupID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var res []struct{ ID int64; Name string }
	for rows.Next() {
		var x struct{ ID int64; Name string }
		if err := rows.Scan(&x.ID, &x.Name); err != nil {
			return nil, err
		}
		res = append(res, x)
	}
	return res, nil
}

func (r *repo) getInviteCode(groupID int64) (string, error) {
	var code string
	err := r.db.QueryRow(`SELECT invite_code FROM groups WHERE id=?`, groupID).Scan(&code)
	return code, err
}

func (r *repo) createExpenseTx(ctx context.Context, st *addExpenseState) (int64, error) {
	tx, err := r.db.BeginTx(ctx, &sql.TxOptions{})
	if err != nil {
		return 0, err
	}
	defer func() {
		if err != nil {
			_ = tx.Rollback()
		}
	}()

	res, err := tx.Exec(`INSERT INTO expenses(group_id,payer_tg_id,description,amount_cents,created_at)
		VALUES(?,?,?,?,?)`, st.GroupID, st.Payer, st.Description, st.AmountCents, nowUnix())
	if err != nil {
		return 0, err
	}
	expenseID, _ := res.LastInsertId()

	// If equal split, precompute equal shares; else use custom shares already filled.
	shares := st.CustomShares
	if st.SplitMode == "equal" {
		shares = map[int64]int64{}
		var count int64 = 0
		for pid, on := range st.Participants {
			if on {
				count++
			}
		}
		if count == 0 {
			return 0, errors.New("no participants")
		}
		share := st.AmountCents / count
		remainder := st.AmountCents - share*count
		for pid, on := range st.Participants {
			if !on {
				continue
			}
			sh := share
			if remainder > 0 {
				sh++
				remainder--
			}
			shares[pid] = sh
		}
	}
	for pid, cents := range shares {
		if _, err := tx.Exec(`INSERT INTO expense_participants(expense_id,participant_tg_id,share_cents)
			VALUES(?,?,?)`, expenseID, pid, cents); err != nil {
			return 0, err
		}
	}

	if err := tx.Commit(); err != nil {
		return 0, err
	}
	return expenseID, nil
}

func (r *repo) deleteExpense(expenseID int64, by int64) error {
	// Soft delete; allow only payer or group owner (omitted for brevity)
	_, err := r.db.Exec(`UPDATE expenses SET deleted=1 WHERE id=?`, expenseID)
	return err
}

// Balances per group: who owes whom.
type pair struct{ From, To int64 }

func (r *repo) computeGroupBalances(groupID int64) (map[pair]int64, error) {
	rows, err := r.db.Query(`
SELECT e.id, e.payer_tg_id, e.amount_cents
FROM expenses e
WHERE e.group_id=? AND e.deleted=0
`, groupID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	type exp struct {
		id    int64
		payer int64
		amt   int64
	}
	var exps []exp
	for rows.Next() {
		var x exp
		if err := rows.Scan(&x.id, &x.payer, &x.amt); err != nil {
			return nil, err
		}
		exps = append(exps, x)
	}

	// For each expense get participants/shares
	sharesByExp := map[int64]map[int64]int64{}
	for _, e := range exps {
		pr, err := r.db.Query(`SELECT participant_tg_id,share_cents FROM expense_participants WHERE expense_id=?`, e.id)
		if err != nil {
			return nil, err
		}
		m := map[int64]int64{}
		for pr.Next() {
			var uid, cents int64
			if err := pr.Scan(&uid, &cents); err != nil {
				pr.Close()
				return nil, err
			}
			m[uid] = cents
		}
		pr.Close()
		sharesByExp[e.id] = m
	}

	// Build pairwise debts: participant owes payer share
	bal := map[pair]int64{}
	for _, e := range exps {
		for uid, s := range sharesByExp[e.id] {
			if uid == e.payer {
				continue
			}
			bal[pair{From: uid, To: e.payer}] += s
		}
	}
	// Net the pairs (A->B, B->A)
	for k := range bal {
		inv := pair{From: k.To, To: k.From}
		if v2, ok := bal[inv]; ok {
			if bal[k] >= v2 {
				bal[k] = bal[k] - v2
				delete(bal, inv)
			} else {
				bal[inv] = v2 - bal[k]
				delete(bal, k)
			}
		}
	}
	return bal, nil
}

// Cross-group netting: aggregate all groups the users share.
func (r *repo) computeCrossGroupNet(uid int64) (map[pair]int64, error) {
	rows, err := r.db.Query(`SELECT group_id FROM group_members WHERE tg_id=?`, uid)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var gids []int64
	for rows.Next() {
		var g int64
		if err := rows.Scan(&g); err != nil {
			return nil, err
		}
		gids = append(gids, g)
	}
	all := map[pair]int64{}
	for _, g := range gids {
		gb, err := r.computeGroupBalances(g)
		if err != nil {
			return nil, err
		}
		for k, v := range gb {
			all[k] += v
		}
	}
	// Net again globally
	for k := range all {
		inv := pair{From: k.To, To: k.From}
		if v2, ok := all[inv]; ok {
			if all[k] >= v2 {
				all[k] = all[k] - v2
				delete(all, inv)
			} else {
				all[inv] = v2 - all[k]
				delete(all, k)
			}
		}
	}
	return all, nil
}

func (r *repo) userName(uid int64) string {
	var n string
	_ = r.db.QueryRow(`SELECT name FROM users WHERE tg_id=?`, uid).Scan(&n)
	if n == "" {
		n = fmt.Sprintf("%d", uid)
	}
	return n
}

// ----- Bot -----

type app struct {
	bot   *tgb.Bot
	db    *sql.DB
	repo  *repo
	state *stateStore
	base  string // bot username for deep links
}

func main() {
	token := os.Getenv("BOT_TOKEN")
	if token == "" {
		log.Fatal("BOT_TOKEN env required")
	}
	db := mustOpenDB()
	defer db.Close()

	bot, err := tgb.NewBot(token, nil)
	if err != nil {
		log.Fatalf("new bot: %v", err)
	}

	me, err := bot.GetMe(nil)
	if err != nil {
		log.Fatalf("getMe: %v", err)
	}

	a := &app{
		bot:   bot,
		db:    db,
		repo:  &repo{db: db},
		state: newStateStore(),
		base:  me.Username,
	}

	// gotgbot v2: create a concrete dispatcher and pass it into the updater
	dispatcher := ext.NewDispatcher(nil)
	updater := ext.NewUpdater(dispatcher, nil)

	// Commands
	dispatcher.AddHandler(handlers.NewCommand("start", a.onStart))
	dispatcher.AddHandler(handlers.NewCommand("newgroup", a.onNewGroup))
	dispatcher.AddHandler(handlers.NewCommand("mygroups", a.onMyGroups))
	dispatcher.AddHandler(handlers.NewCommand("invite", a.onInvite))
	dispatcher.AddHandler(handlers.NewCommand("addexpense", a.onAddExpense))
	dispatcher.AddHandler(handlers.NewCommand("cancel", a.onCancel))
	dispatcher.AddHandler(handlers.NewCommand("balances", a.onBalances))
	dispatcher.AddHandler(handlers.NewCommand("net", a.onNet))
	dispatcher.AddHandler(handlers.NewCommand("delexpense", a.onDelExpense))
	dispatcher.AddHandler(handlers.NewCommand("confirm", a.onConfirm))

	// Callbacks for inline buttons & fallback text
	dispatcher.AddHandler(handlers.NewCallback(callbackquery.All, a.cb))
	dispatcher.AddHandler(handlers.NewMessage(message.Text, a.onText))

	log.Printf("Starting bot @%s ...", me.Username)
	if err := updater.StartPolling(bot, &ext.PollingOpts{DropPendingUpdates: true}); err != nil {
		log.Fatalf("start polling: %v", err)
	}
	updater.Idle()
}

// ----- Handlers -----

func (a *app) onStart(b *tgb.Bot, ctx *ext.Context) error {
	uid := ctx.EffectiveUser.Id
	name := getBestName(ctx)
	_ = a.repo.upsertUser(uid, name)

	args := ctx.Args()
	if len(args) == 1 {
		code := args[0]
		if gid, title, err := a.repo.joinByCode(code, uid); err == nil {
			_, _ = ctx.EffectiveChat.SendMessage(b, fmt.Sprintf("Вы присоединились к группе #%d: %s", gid, title), nil)
			return nil
		}
	}

	msg := "Привет! Я помогу делить траты в поездках.\n" +
		"Команды:\n" +
		"/newgroup <название> — создать группу\n" +
		"/mygroups — мои группы\n" +
		"/invite <group_id> — ссылка-приглашение\n" +
		"/addexpense <group_id> — добавить трату (запущу мастер)\n" +
		"/balances <group_id> — балансы в группе\n" +
		"/net — взаимозачёт по всем группам\n" +
		"/delexpense <expense_id> — удалить трату\n" +
		"/confirm <from_id> <to_id> <amount> — подтвердить оплату"
	_, _ = ctx.EffectiveChat.SendMessage(b, msg, nil)
	return nil
}

func getBestName(ctx *ext.Context) string {
	u := ctx.EffectiveUser
	if u.Username != "" {
		return "@" + u.Username
	}
	if u.FirstName != "" || u.LastName != "" {
		return strings.TrimSpace(u.FirstName + " " + u.LastName)
	}
	return fmt.Sprintf("%d", u.Id)
}

func (a *app) onNewGroup(b *tgb.Bot, ctx *ext.Context) error {
	if len(ctx.Args()) == 0 {
		_, _ = ctx.EffectiveChat.SendMessage(b, "Использование: /newgroup <название>", nil)
		return nil
	}
	title := strings.Join(ctx.Args(), " ")
	uid := ctx.EffectiveUser.Id
	_ = a.repo.upsertUser(uid, getBestName(ctx))
	gid, code, err := a.repo.createGroup(title, uid)
	if err != nil {
		return err
	}
	link := fmt.Sprintf("https://t.me/%s?start=%s", a.base, code)
	_, _ = ctx.EffectiveChat.SendMessage(b, fmt.Sprintf("Группа #%d создана: %s\nПриглашение: %s", gid, title, link), nil)
	return nil
}

func (a *app) onMyGroups(b *tgb.Bot, ctx *ext.Context) error {
	uid := ctx.EffectiveUser.Id
	gs, err := a.repo.listUserGroups(uid)
	if err != nil {
		return err
	}
	if len(gs) == 0 {
		_, _ = ctx.EffectiveChat.SendMessage(b, "У вас пока нет групп. Создайте: /newgroup <название>", nil)
		return nil
	}
	var sb strings.Builder
	sb.WriteString("Ваши группы:\n")
	for _, g := range gs {
		sb.WriteString(fmt.Sprintf("- #%d: %s\n", g.ID, g.Title))
	}
	_, _ = ctx.EffectiveChat.SendMessage(b, sb.String(), nil)
	return nil
}

func (a *app) onInvite(b *tgb.Bot, ctx *ext.Context) error {
	if len(ctx.Args()) != 1 {
		_, _ = ctx.EffectiveChat.SendMessage(b, "Использование: /invite <group_id>", nil)
		return nil
	}
	gid, _ := strconv.ParseInt(ctx.Args()[0], 10, 64)
	code, err := a.repo.getInviteCode(gid)
	if err != nil {
		return err
	}
	link := fmt.Sprintf("https://t.me/%s?start=%s", a.base, code)
	_, _ = ctx.EffectiveChat.SendMessage(b, fmt.Sprintf("Приглашение в группу #%d:\n%s", gid, link), nil)
	return nil
}

func (a *app) onAddExpense(b *tgb.Bot, ctx *ext.Context) error {
	if len(ctx.Args()) < 1 {
		_, _ = ctx.EffectiveChat.SendMessage(b, "Использование: /addexpense <group_id>\nДалее пришлите сумму и описание, например:\n1000 ужин", nil)
		return nil
	}
	gid, err := strconv.ParseInt(ctx.Args()[0], 10, 64)
	if err != nil {
		return err
	}
	uid := ctx.EffectiveUser.Id
	a.state.Set(uid, &addExpenseState{
		GroupID:      gid,
		Participants: map[int64]bool{},
		CustomShares: map[int64]int64{},
		Step:         "await_amount_desc",
	})
	_, _ = ctx.EffectiveChat.SendMessage(b, "Пришлите сумму и описание в одном сообщении, например:\n1500 такси из аэропорта", nil)
	return nil
}

func (a *app) onCancel(b *tgb.Bot, ctx *ext.Context) error {
	uid := ctx.EffectiveUser.Id
	a.state.Del(uid)
	_, _ = ctx.EffectiveChat.SendMessage(b, "Окей, отменил.", nil)
	return nil
}

func (a *app) onText(b *tgb.Bot, ctx *ext.Context) error {
	uid := ctx.EffectiveUser.Id
	st, ok := a.state.Get(uid)
	if !ok {
		return nil // not in a flow
	}
	txt := strings.TrimSpace(ctx.EffectiveMessage.Text)
	switch st.Step {
	case "await_amount_desc":
		parts := strings.Fields(txt)
		if len(parts) == 0 {
			_, _ = ctx.EffectiveChat.SendMessage(b, "Нужно прислать сумму и описание. Пример: 1200 обед", nil)
			return nil
		}
		amt, err := centsFromStr(parts[0])
		if err != nil || amt <= 0 {
			_, _ = ctx.EffectiveChat.SendMessage(b, "Сумма не распознана. Пример: 1200 обед", nil)
			return nil
		}
		st.AmountCents = amt
		if len(parts) > 1 {
			st.Description = strings.TrimSpace(strings.TrimPrefix(txt, parts[0]))
		} else {
			st.Description = "Без описания"
		}
		st.Step = "choose_payer"
		a.state.Set(uid, st)
		return a.askPayer(b, ctx, st)
	case "await_custom_share":
		if len(st.CustomLeft) == 0 {
			st.Step = "confirm"
			a.state.Set(uid, st)
			return a.finalizeExpense(b, ctx, st)
		}
		nextUID := st.CustomLeft[0]
		amt, err := centsFromStr(txt)
		if err != nil || amt < 0 {
			_, _ = ctx.EffectiveChat.SendMessage(b, "Сумма не распознана. Пришлите число, напр. 350.50", nil)
			return nil
		}
		st.CustomShares[nextUID] = amt
		st.CustomLeft = st.CustomLeft[1:]
		a.state.Set(uid, st)
		if len(st.CustomLeft) == 0 {
			sum := int64(0)
			for _, v := range st.CustomShares {
				sum += v
			}
			if sum != st.AmountCents {
				_, _ = ctx.EffectiveChat.SendMessage(b, fmt.Sprintf("Сумма долей %s ≠ общей суммы %s. Пришлите /cancel и начните заново или исправьте последнюю сумму.", formatCents(sum), formatCents(st.AmountCents)), nil)
				return nil
			}
			return a.finalizeExpense(b, ctx, st)
		}
		return a.askNextCustom(b, ctx, st)
	default:
		// ignore
	}
	return nil
}

func (a *app) askPayer(b *tgb.Bot, ctx *ext.Context, st *addExpenseState) error {
	members, err := a.repo.listMembers(st.GroupID)
	if err != nil || len(members) == 0 {
		_, _ = ctx.EffectiveChat.SendMessage(b, "В группе пока нет участников.", nil)
		return nil
	}
	var btns [][]tgb.InlineKeyboardButton
	for _, m := range members {
		btns = append(btns, []tgb.InlineKeyboardButton{
			{Text: fmt.Sprintf("Плательщик: %s", m.Name), CallbackData: fmt.Sprintf("payer|%d", m.ID)},
		})
	}
	_, _ = ctx.EffectiveChat.SendMessage(b, fmt.Sprintf("Сумма: %s\nОписание: %s\nВыберите плательщика:", formatCents(st.AmountCents), st.Description),
		&tgb.SendMessageOpts{ReplyMarkup: &tgb.InlineKeyboardMarkup{InlineKeyboard: btns}})
	return nil
}

func (a *app) cb(b *tgb.Bot, ctx *ext.Context) error {
	data := ctx.CallbackQuery.Data
	uid := ctx.EffectiveUser.Id
	st, ok := a.state.Get(uid)
	if !ok {
		_, _ = ctx.CallbackQuery.Answer(b, &tgb.AnswerCallbackQueryOpts{})
		return nil
	}

	switch {
	case strings.HasPrefix(data, "payer|"):
		idStr := strings.TrimPrefix(data, "payer|")
		payer, _ := strconv.ParseInt(idStr, 10, 64)
		st.Payer = payer
		st.Step = "choose_participants"
		a.state.Set(uid, st)
		_, _ = ctx.CallbackQuery.Answer(b, &tgb.AnswerCallbackQueryOpts{Text: "Плательщик выбран"})
		return a.askParticipants(b, ctx, st)
	case strings.HasPrefix(data, "toggle|"):
		idStr := strings.TrimPrefix(data, "toggle|")
		pid, _ := strconv.ParseInt(idStr, 10, 64)
		st.Participants[pid] = !st.Participants[pid]
		a.state.Set(uid, st)
		_, _ = ctx.CallbackQuery.Answer(b, &tgb.AnswerCallbackQueryOpts{})
		return a.askParticipants(b, ctx, st) // refresh buttons
	case strings.HasPrefix(data, "part_done"):
		st.Step = "choose_split"
		a.state.Set(uid, st)
		_, _ = ctx.CallbackQuery.Answer(b, &tgb.AnswerCallbackQueryOpts{})
		return a.askSplitMode(b, ctx, st)
	case strings.HasPrefix(data, "split|equal"):
		st.SplitMode = "equal"
		st.Step = "confirm"
		a.state.Set(uid, st)
		_, _ = ctx.CallbackQuery.Answer(b, &tgb.AnswerCallbackQueryOpts{Text: "Поровну"})
		return a.finalizeExpense(b, ctx, st)
	case strings.HasPrefix(data, "split|custom"):
		st.SplitMode = "custom"
		st.CustomLeft = []int64{}
		for pid, on := range st.Participants {
			if on {
				st.CustomLeft = append(st.CustomLeft, pid)
			}
		}
		st.Step = "await_custom_share"
		a.state.Set(uid, st)
		_, _ = ctx.CallbackQuery.Answer(b, &tgb.AnswerCallbackQueryOpts{Text: "Свои доли"})
		return a.askNextCustom(b, ctx, st)
	}

	_, _ = ctx.CallbackQuery.Answer(b, &tgb.AnswerCallbackQueryOpts{})
	return nil
}

func (a *app) askParticipants(b *tgb.Bot, ctx *ext.Context, st *addExpenseState) error {
	members, err := a.repo.listMembers(st.GroupID)
	if err != nil {
		return err
	}
	var rows [][]tgb.InlineKeyboardButton
	for _, m := range members {
		on := st.Participants[m.ID]
		label := m.Name
		if on {
			label = "✅ " + label
		} else {
			label = "☑️ " + label
		}
		rows = append(rows, []tgb.InlineKeyboardButton{
			{Text: label, CallbackData: fmt.Sprintf("toggle|%d", m.ID)},
		})
	}
	// Done button
	rows = append(rows, []tgb.InlineKeyboardButton{
		{Text: "Готово →", CallbackData: "part_done"},
	})
	_, _ = ctx.EffectiveChat.SendMessage(b, "Выберите участников (нажимайте, чтобы включить/исключить), затем «Готово».",
		&tgb.SendMessageOpts{ReplyMarkup: &tgb.InlineKeyboardMarkup{InlineKeyboard: rows}})
	return nil
}

func (a *app) askSplitMode(b *tgb.Bot, ctx *ext.Context, st *addExpenseState) error {
	btns := [][]tgb.InlineKeyboardButton{
		{{Text: "Поровну", CallbackData: "split|equal"}},
		{{Text: "Свои доли", CallbackData: "split|custom"}},
	}
	_, _ = ctx.EffectiveChat.SendMessage(b, "Как разделить?", &tgb.SendMessageOpts{
		ReplyMarkup: &tgb.InlineKeyboardMarkup{InlineKeyboard: btns},
	})
	return nil
}

func (a *app) askNextCustom(b *tgb.Bot, ctx *ext.Context, st *addExpenseState) error {
	if len(st.CustomLeft) == 0 {
		return a.finalizeExpense(b, ctx, st)
	}
	uid := st.CustomLeft[0]
	name := a.repo.userName(uid)
	_, _ = ctx.EffectiveChat.SendMessage(b, fmt.Sprintf("Введите сумму для участника %s (остаток распределения — %s):",
		name, formatCents(st.AmountCents-sumMap(st.CustomShares))), nil)
	return nil
}

func sumMap(m map[int64]int64) int64 {
	var s int64
	for _, v := range m {
		s += v
	}
	return s
}

func (a *app) finalizeExpense(b *tgb.Bot, ctx *ext.Context, st *addExpenseState) error {
	// If custom, validate totals
	if st.SplitMode == "custom" {
		if sumMap(st.CustomShares) != st.AmountCents {
			_, _ = ctx.EffectiveChat.SendMessage(b, "Сумма долей не равна общей сумме. Отмените /cancel и начните заново.", nil)
			return nil
		}
	}
	id, err := a.repo.createExpenseTx(context.Background(), st)
	if err != nil {
		return err
	}
	a.state.Del(ctx.EffectiveUser.Id)
	_, _ = ctx.EffectiveChat.SendMessage(b, fmt.Sprintf("Трата #%d добавлена. Сумма %s, плательщик %s.", id, formatCents(st.AmountCents), a.repo.userName(st.Payer)), nil)
	return nil
}

func (a *app) onBalances(b *tgb.Bot, ctx *ext.Context) error {
	if len(ctx.Args()) != 1 {
		_, _ = ctx.EffectiveChat.SendMessage(b, "Использование: /balances <group_id>", nil)
		return nil
	}
	gid, _ := strconv.ParseInt(ctx.Args()[0], 10, 64)
	bal, err := a.repo.computeGroupBalances(gid)
	if err != nil {
		return err
	}
	if len(bal) == 0 {
		_, _ = ctx.EffectiveChat.SendMessage(b, "В группе нет долгов — ура!", nil)
		return nil
	}
	var sb strings.Builder
	sb.WriteString(fmt.Sprintf("Балансы группы #%d:\n", gid))
	for k, v := range bal {
		if v <= 0 {
			continue
		}
		sb.WriteString(fmt.Sprintf("%s → %s: %s\n", a.repo.userName(k.From), a.repo.userName(k.To), formatCents(v)))
	}
	_, _ = ctx.EffectiveChat.SendMessage(b, sb.String(), nil)
	return nil
}

func (a *app) onNet(b *tgb.Bot, ctx *ext.Context) error {
	uid := ctx.EffectiveUser.Id
	all, err := a.repo.computeCrossGroupNet(uid)
	if err != nil {
		return err
	}
	if len(all) == 0 {
		_, _ = ctx.EffectiveChat.SendMessage(b, "По всем группам долгов нет.", nil)
		return nil
	}
	var sb strings.Builder
	sb.WriteString("Взаимозачёт по всем группам:\n")
	for k, v := range all {
		if v <= 0 {
			continue
		}
		sb.WriteString(fmt.Sprintf("%s → %s: %s\n", a.repo.userName(k.From), a.repo.userName(k.To), formatCents(v)))
	}
	_, _ = ctx.EffectiveChat.SendMessage(b, sb.String(), nil)
	return nil
}

func (a *app) onDelExpense(b *tgb.Bot, ctx *ext.Context) error {
	if len(ctx.Args()) != 1 {
		_, _ = ctx.EffectiveChat.SendMessage(b, "Использование: /delexpense <expense_id>", nil)
		return nil
	}
	id, _ := strconv.ParseInt(ctx.Args()[0], 10, 64)
	if err := a.repo.deleteExpense(id, ctx.EffectiveUser.Id); err != nil {
		return err
	}
	_, _ = ctx.EffectiveChat.SendMessage(b, fmt.Sprintf("Трата #%d удалена.", id), nil)
	return nil
}

func (a *app) onConfirm(b *tgb.Bot, ctx *ext.Context) error {
	// /confirm <from_id> <to_id> <amount>
	if len(ctx.Args()) != 3 {
		_, _ = ctx.EffectiveChat.SendMessage(b, "Использование: /confirm <from_id> <to_id> <amount>", nil)
		return nil
	}
	fromID, _ := strconv.ParseInt(ctx.Args()[0], 10, 64)
	toID, _ := strconv.ParseInt(ctx.Args()[1], 10, 64)
	cents, err := centsFromStr(ctx.Args()[2])
	if err != nil {
		_, _ = ctx.EffectiveChat.SendMessage(b, "Сумма не распознана", nil)
		return nil
	}
	// Record settlement (cross-group by default)
	_, err = a.db.Exec(`INSERT INTO settlements(group_id,from_tg_id,to_tg_id,amount_cents,confirmed_by_to,created_at)
		VALUES(NULL,?,?,?,?,?)`, fromID, toID, cents, 1, nowUnix())
	if err != nil {
		return err
	}
	_, _ = ctx.EffectiveChat.SendMessage(b, "Оплата подтверждена.", nil)
	return nil
}
