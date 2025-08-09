package main

// Expense-splitting Telegram bot (gotgbot v2 + SQLite, pure Go).
// Изменения:
// - Инвайт по ссылке: https://t.me/<bot>?start=join_<GROUP_ID>
// - Команда /join <group_id>
// - /mygroups, /invite, /addexpense — с пагинацией по группам
// - Детали группы: балансы (кому ТЫ должен), инвайт-ссылка, список трат (пагинация), удаление траты, подтверждение оплаты
// - /balances: сводка "кому ты должен" по всем группам
//
// ENV:
//   BOT_TOKEN=<telegram bot token>
//   DB_PATH=./data.db

import (
	"context"
	"crypto/rand"
	"database/sql"
	"encoding/base64"
	"errors"
	"fmt"
	"log"
	"os"
	"sort"
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

// ---------- DB ----------

func mustOpenDB() *sql.DB {
	path := getenv("DB_PATH", "./data.db")
	db, err := sql.Open("sqlite", path)
	if err != nil {
		log.Fatalf("open db: %v", err)
	}
	db.SetMaxOpenConns(1)
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

CREATE TABLE IF NOT EXISTS settlements(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER,
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

// ---------- Utils ----------

func getenv(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}

func nowUnix() int64 { return time.Now().Unix() }

func centsFromStr(s string) (int64, error) {
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

// ---------- State ----------

type addExpenseState struct {
	GroupID      int64
	AmountCents  int64
	Description  string
	Payer        int64
	Participants map[int64]bool
	SplitMode    string
	CustomLeft   []int64
	CustomShares map[int64]int64
	Step         string
}

type stateStore struct {
	mu    sync.RWMutex
	addEx map[int64]*addExpenseState // user tg id -> state
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

// ---------- Repo ----------

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

// Новое: join по group_id
func (r *repo) joinByGroupID(gid int64, uid int64) (string, error) {
	var title string
	if err := r.db.QueryRow(`SELECT title FROM groups WHERE id=?`, gid).Scan(&title); err != nil {
		return "", err
	}
	_, err := r.db.Exec(`INSERT INTO group_members(group_id,tg_id,role) VALUES(?,?,?)
		ON CONFLICT(group_id,tg_id) DO NOTHING`, gid, uid, "member")
	return title, err
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

	shares := st.CustomShares
	if st.SplitMode == "equal" {
		shares = map[int64]int64{}
		var count int64 = 0
		for _, on := range st.Participants { // pid не нужен
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

func (r *repo) deleteExpense(expenseID int64) error {
	_, err := r.db.Exec(`UPDATE expenses SET deleted=1 WHERE id=?`, expenseID)
	return err
}

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

	bal := map[pair]int64{}
	for _, e := range exps {
		for uid, s := range sharesByExp[e.id] {
			if uid == e.payer {
				continue
			}
			bal[pair{From: uid, To: e.payer}] += s
		}
	}
	// Net pairs
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

func (r *repo) listGroupExpenses(groupID int64, limit, offset int) ([]struct {
	ID          int64
	Payer       int64
	AmountCents int64
	Desc        string
	CreatedAt   int64
	Deleted     int
}, error) {
	rows, err := r.db.Query(`SELECT id,payer_tg_id,amount_cents,description,created_at,deleted
		FROM expenses WHERE group_id=? AND deleted=0 ORDER BY id DESC LIMIT ? OFFSET ?`, groupID, limit, offset)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var res []struct {
		ID          int64
		Payer       int64
		AmountCents int64
		Desc        string
		CreatedAt   int64
		Deleted     int
	}
	for rows.Next() {
		var x struct {
			ID          int64
			Payer       int64
			AmountCents int64
			Desc        string
			CreatedAt   int64
			Deleted     int
		}
		if err := rows.Scan(&x.ID, &x.Payer, &x.AmountCents, &x.Desc, &x.CreatedAt, &x.Deleted); err != nil {
			return nil, err
		}
		res = append(res, x)
	}
	return res, nil
}

func (r *repo) countGroupExpenses(groupID int64) (int, error) {
	var n int
	err := r.db.QueryRow(`SELECT COUNT(*) FROM expenses WHERE group_id=? AND deleted=0`, groupID).Scan(&n)
	return n, err
}

// ---------- Bot ----------

type app struct {
	bot   *tgb.Bot
	db    *sql.DB
	repo  *repo
	state *stateStore
	base  string // bot username
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

	dispatcher := ext.NewDispatcher(nil)
	updater := ext.NewUpdater(dispatcher, nil)

	// Commands
	dispatcher.AddHandler(handlers.NewCommand("start", a.onStart))
	dispatcher.AddHandler(handlers.NewCommand("newgroup", a.onNewGroup))
	dispatcher.AddHandler(handlers.NewCommand("mygroups", a.onMyGroups))
	dispatcher.AddHandler(handlers.NewCommand("invite", a.onInvite))
	dispatcher.AddHandler(handlers.NewCommand("addexpense", a.onAddExpense))
	dispatcher.AddHandler(handlers.NewCommand("balances", a.onBalances))
	dispatcher.AddHandler(handlers.NewCommand("join", a.onJoin)) // новое

	// Inline callbacks & text
	dispatcher.AddHandler(handlers.NewCallback(callbackquery.All, a.cb))
	dispatcher.AddHandler(handlers.NewMessage(message.Text, a.onText))

	log.Printf("Starting bot @%s ...", me.Username)
	if err := updater.StartPolling(bot, &ext.PollingOpts{DropPendingUpdates: true}); err != nil {
		log.Fatalf("start polling: %v", err)
	}
	updater.Idle()
}

// ---------- Handlers ----------

func (a *app) onStart(b *tgb.Bot, ctx *ext.Context) error {
	uid := ctx.EffectiveUser.Id
	name := getBestName(ctx)
	_ = a.repo.upsertUser(uid, name)

	// Deep-link обработка
	args := ctx.Args()
	if len(args) == 1 {
		p := args[0]
		switch {
		case strings.HasPrefix(p, "join_"):
			gid, _ := strconv.ParseInt(strings.TrimPrefix(p, "join_"), 10, 64)
			if title, err := a.repo.joinByGroupID(gid, uid); err == nil {
				_, _ = ctx.EffectiveChat.SendMessage(b, fmt.Sprintf("Вы присоединились к группе #%d: %s", gid, title), nil)
				return nil
			}
		default:
			// совместимость со старыми кодами
			if gid, title, err := a.repo.joinByCode(p, uid); err == nil {
				_, _ = ctx.EffectiveChat.SendMessage(b, fmt.Sprintf("Вы присоединились к группе #%d: %s", gid, title), nil)
				return nil
			}
		}
	}

	msg := "Привет! Я помогу делить траты в поездках.\n" +
		"Команды:\n" +
		"/newgroup <название> — создать группу\n" +
		"/mygroups — группы и детали\n" +
		"/invite — получить инвайт-ссылку для группы\n" +
		"/addexpense — добавить трату (сначала выбери группу)\n" +
		"/balances — кому ты должен по группам\n" +
		"/join <group_id> — присоединиться к группе"
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
	// оставляем старую ссылку + показываем новую (join_gid)
	oldLink := fmt.Sprintf("https://t.me/%s?start=%s", a.base, code)
	newLink := fmt.Sprintf("https://t.me/%s?start=join_%d", a.base, gid)
	_, _ = ctx.EffectiveChat.SendMessage(b, fmt.Sprintf("Группа #%d создана: %s\nСтарая ссылка: %s\nНовая ссылка: %s", gid, title, oldLink, newLink), nil)
	return nil
}

// ---------- Pagination helpers ----------

const groupsPerPage = 5
const expensesPerPage = 10

func (a *app) buildGroupsPageKeyboard(uid int64, page int, mode string) (*tgb.InlineKeyboardMarkup, int, error) {
	// mode in {"mg","inv","ae"}
	gs, err := a.repo.listUserGroups(uid)
	if err != nil {
		return nil, 0, err
	}
	total := len(gs)
	if total == 0 {
		return &tgb.InlineKeyboardMarkup{InlineKeyboard: [][]tgb.InlineKeyboardButton{}}, 0, nil
	}
	// stable order
	sort.Slice(gs, func(i, j int) bool { return gs[i].ID > gs[j].ID })

	start := page * groupsPerPage
	if start >= total {
		page = 0
		start = 0
	}
	end := start + groupsPerPage
	if end > total {
		end = total
	}
	rows := [][]tgb.InlineKeyboardButton{}
	for _, g := range gs[start:end] {
		var cb string
		switch mode {
		case "mg":
			cb = fmt.Sprintf("mgsel|%d", g.ID)
		case "inv":
			cb = fmt.Sprintf("invsel|%d", g.ID)
		case "ae":
			cb = fmt.Sprintf("aesel|%d", g.ID)
		}
		rows = append(rows, []tgb.InlineKeyboardButton{{Text: fmt.Sprintf("#%d: %s", g.ID, g.Title), CallbackData: cb}})
	}
	// nav
	nav := []tgb.InlineKeyboardButton{}
	if page > 0 {
		nav = append(nav, tgb.InlineKeyboardButton{Text: "« Назад", CallbackData: fmt.Sprintf("%s|p:%d", mode, page-1)})
	}
	if end < total {
		nav = append(nav, tgb.InlineKeyboardButton{Text: "Вперёд »", CallbackData: fmt.Sprintf("%s|p:%d", mode, page+1)})
	}
	if len(nav) > 0 {
		rows = append(rows, nav)
	}
	return &tgb.InlineKeyboardMarkup{InlineKeyboard: rows}, total, nil
}

func (a *app) onMyGroups(b *tgb.Bot, ctx *ext.Context) error {
	markup, total, err := a.buildGroupsPageKeyboard(ctx.EffectiveUser.Id, 0, "mg")
	if err != nil {
		return err
	}
	if total == 0 {
		_, _ = ctx.EffectiveChat.SendMessage(b, "У вас нет групп. Создайте: /newgroup <название>", nil)
		return nil
	}
	_, _ = ctx.EffectiveChat.SendMessage(b, "Выберите группу:", &tgb.SendMessageOpts{ReplyMarkup: markup})
	return nil
}

func (a *app) onInvite(b *tgb.Bot, ctx *ext.Context) error {
	markup, total, err := a.buildGroupsPageKeyboard(ctx.EffectiveUser.Id, 0, "inv")
	if err != nil {
		return err
	}
	if total == 0 {
		_, _ = ctx.EffectiveChat.SendMessage(b, "У вас нет групп. Создайте: /newgroup <название>", nil)
		return nil
	}
	_, _ = ctx.EffectiveChat.SendMessage(b, "Выберите группу для приглашения:", &tgb.SendMessageOpts{ReplyMarkup: markup})
	return nil
}

func (a *app) onAddExpense(b *tgb.Bot, ctx *ext.Context) error {
	markup, total, err := a.buildGroupsPageKeyboard(ctx.EffectiveUser.Id, 0, "ae")
	if err != nil {
		return err
	}
	if total == 0 {
		_, _ = ctx.EffectiveChat.SendMessage(b, "У вас нет групп. Создайте: /newgroup <название>", nil)
		return nil
	}
	_, _ = ctx.EffectiveChat.SendMessage(b, "Выберите группу для добавления траты:", &tgb.SendMessageOpts{ReplyMarkup: markup})
	return nil
}

// ---------- Group details, expenses, payments ----------

func (a *app) sendGroupDetails(b *tgb.Bot, ctx *ext.Context, gid int64) error {
	uid := ctx.EffectiveUser.Id

	// Новая join-ссылка
	link := fmt.Sprintf("https://t.me/%s?start=join_%d", a.base, gid)

	// balances (только где ТЫ должник)
	bal, err := a.repo.computeGroupBalances(gid)
	if err != nil {
		return err
	}
	type debt struct {
		To   int64
		Amnt int64
	}
	var debts []debt
	for k, v := range bal {
		if v > 0 && k.From == uid {
			debts = append(debts, debt{To: k.To, Amnt: v})
		}
	}
	sort.Slice(debts, func(i, j int) bool { return debts[i].Amnt > debts[j].Amnt })

	var lines []string
	lines = append(lines, fmt.Sprintf("Группа #%d", gid))
	lines = append(lines, fmt.Sprintf("Приглашение: %s", link))
	if len(debts) == 0 {
		lines = append(lines, "Вы никому не должны в этой группе 🎉")
	} else {
		lines = append(lines, "Ваши долги по группе:")
	}

	// keyboard
	rows := [][]tgb.InlineKeyboardButton{
		{{Text: "Список трат", CallbackData: fmt.Sprintf("explist|%d|p:%d", gid, 0)}},
		{{Text: "Назад к моим группам", CallbackData: "mg|p:0"}},
	}

	// per-debt confirm
	for _, d := range debts {
		btn := tgb.InlineKeyboardButton{
			Text:         fmt.Sprintf("Подтвердить оплату → %s (%s)", a.repo.userName(d.To), formatCents(d.Amnt)),
			CallbackData: fmt.Sprintf("pay|gid:%d|to:%d|amt:%d", gid, d.To, d.Amnt),
		}
		rows = append([][]tgb.InlineKeyboardButton{{btn}}, rows...)
		lines = append(lines, fmt.Sprintf("Вы должны %s: %s", a.repo.userName(d.To), formatCents(d.Amnt)))
	}

	_, _ = ctx.EffectiveChat.SendMessage(b, strings.Join(lines, "\n"), &tgb.SendMessageOpts{
		ReplyMarkup: &tgb.InlineKeyboardMarkup{InlineKeyboard: rows},
	})
	return nil
}

func (a *app) sendInviteForGroup(b *tgb.Bot, ctx *ext.Context, gid int64) error {
	url := fmt.Sprintf("https://t.me/%s?start=join_%d", a.base, gid)
	text := fmt.Sprintf("Приглашение в группу #%d:\n%s", gid, url)
	_, _ = ctx.EffectiveChat.SendMessage(b, text, &tgb.SendMessageOpts{
		ReplyMarkup: &tgb.InlineKeyboardMarkup{InlineKeyboard: [][]tgb.InlineKeyboardButton{
			{{Text: "Открыть бота по ссылке", Url: url}},
		}},
	})
	return nil
}

func (a *app) sendExpensesPage(b *tgb.Bot, ctx *ext.Context, gid int64, page int) error {
	total, err := a.repo.countGroupExpenses(gid)
	if err != nil {
		return err
	}
	offset := page * expensesPerPage
	if offset >= total && total > 0 {
		page = 0
		offset = 0
	}

	items, err := a.repo.listGroupExpenses(gid, expensesPerPage, offset)
	if err != nil {
		return err
	}
	if total == 0 {
		_, _ = ctx.EffectiveChat.SendMessage(b, "В группе пока нет трат.", &tgb.SendMessageOpts{
			ReplyMarkup: &tgb.InlineKeyboardMarkup{InlineKeyboard: [][]tgb.InlineKeyboardButton{
				{{Text: "Назад к группе", CallbackData: fmt.Sprintf("mgsel|%d", gid)}},
			}},
		})
		return nil
	}

	var sb strings.Builder
	sb.WriteString(fmt.Sprintf("Траты группы #%d (страница %d):\n", gid, page+1))
	for _, it := range items {
		sb.WriteString(fmt.Sprintf("• #%d %s — %s (плательщик: %s)\n",
			it.ID, it.Desc, formatCents(it.AmountCents), a.repo.userName(it.Payer)))
	}
	rows := [][]tgb.InlineKeyboardButton{}
	for _, it := range items {
		rows = append(rows, []tgb.InlineKeyboardButton{
			{Text: fmt.Sprintf("Удалить #%d", it.ID), CallbackData: fmt.Sprintf("expdel|%d|gid:%d|p:%d", it.ID, gid, page)},
		})
	}
	nav := []tgb.InlineKeyboardButton{
		{Text: "Назад к группе", CallbackData: fmt.Sprintf("mgsel|%d", gid)},
	}
	if page > 0 {
		nav = append([]tgb.InlineKeyboardButton{{Text: "« Назад", CallbackData: fmt.Sprintf("explist|%d|p:%d", gid, page-1)}}, nav...)
	}
	if offset+expensesPerPage < total {
		nav = append(nav, tgb.InlineKeyboardButton{Text: "Вперёд »", CallbackData: fmt.Sprintf("explist|%d|p:%d", gid, page+1)})
	}
	rows = append(rows, nav)

	_, _ = ctx.EffectiveChat.SendMessage(b, sb.String(), &tgb.SendMessageOpts{
		ReplyMarkup: &tgb.InlineKeyboardMarkup{InlineKeyboard: rows},
	})
	return nil
}

// ---------- Text flow (add expense) ----------

func (a *app) onText(b *tgb.Bot, ctx *ext.Context) error {
	uid := ctx.EffectiveUser.Id
	st, ok := a.state.Get(uid)
	if !ok {
		return nil
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
	}
	return nil
}

// ---------- Callback router ----------

func (a *app) cb(b *tgb.Bot, ctx *ext.Context) error {
	data := ctx.CallbackQuery.Data
	uid := ctx.EffectiveUser.Id

	switch {
	// paging: groups
	case strings.HasPrefix(data, "mg|p:"):
		page := mustAtoi(strings.TrimPrefix(data, "mg|p:"))
		markup, _, err := a.buildGroupsPageKeyboard(uid, page, "mg")
		if err == nil {
			_, _ = ctx.EffectiveChat.SendMessage(b, "Выберите группу:", &tgb.SendMessageOpts{ReplyMarkup: markup})
		}
		_, _ = ctx.CallbackQuery.Answer(b, nil)
		return nil
	case strings.HasPrefix(data, "inv|p:"):
		page := mustAtoi(strings.TrimPrefix(data, "inv|p:"))
		markup, _, err := a.buildGroupsPageKeyboard(uid, page, "inv")
		if err == nil {
			_, _ = ctx.EffectiveChat.SendMessage(b, "Выберите группу для приглашения:", &tgb.SendMessageOpts{ReplyMarkup: markup})
		}
		_, _ = ctx.CallbackQuery.Answer(b, nil)
		return nil
	case strings.HasPrefix(data, "ae|p:"):
		page := mustAtoi(strings.TrimPrefix(data, "ae|p:"))
		markup, _, err := a.buildGroupsPageKeyboard(uid, page, "ae")
		if err == nil {
			_, _ = ctx.EffectiveChat.SendMessage(b, "Выберите группу для добавления траты:", &tgb.SendMessageOpts{ReplyMarkup: markup})
		}
		_, _ = ctx.CallbackQuery.Answer(b, nil)
		return nil

	// selections
	case strings.HasPrefix(data, "mgsel|"):
		gid := mustAtoi64(strings.TrimPrefix(data, "mgsel|"))
		_ = a.sendGroupDetails(b, ctx, gid)
		_, _ = ctx.CallbackQuery.Answer(b, nil)
		return nil
	case strings.HasPrefix(data, "invsel|"):
		gid := mustAtoi64(strings.TrimPrefix(data, "invsel|"))
		_ = a.sendInviteForGroup(b, ctx, gid)
		_, _ = ctx.CallbackQuery.Answer(b, &tgb.AnswerCallbackQueryOpts{Text: "Ссылка отправлена"})
		return nil
	case strings.HasPrefix(data, "aesel|"):
		gid := mustAtoi64(strings.TrimPrefix(data, "aesel|"))
		a.state.Set(uid, &addExpenseState{
			GroupID:      gid,
			Participants: map[int64]bool{},
			CustomShares: map[int64]int64{},
			Step:         "await_amount_desc",
		})
		_, _ = ctx.EffectiveChat.SendMessage(b, fmt.Sprintf("Группа #%d выбрана. Пришлите сумму и описание одним сообщением, напр.:\n1500 такси из аэропорта", gid), nil)
		_, _ = ctx.CallbackQuery.Answer(b, &tgb.AnswerCallbackQueryOpts{Text: "Группа выбрана"})
		return nil

	// expenses
	case strings.HasPrefix(data, "explist|"):
		parts := strings.Split(data, "|") // explist|<gid>|p:<n>
		if len(parts) >= 3 && strings.HasPrefix(parts[2], "p:") {
			gid := mustAtoi64(parts[1])
			page := mustAtoi(strings.TrimPrefix(parts[2], "p:"))
			_ = a.sendExpensesPage(b, ctx, gid, page)
		}
		_, _ = ctx.CallbackQuery.Answer(b, nil)
		return nil

	case strings.HasPrefix(data, "expdel|"):
		// expdel|<eid>|gid:<gid>|p:<page>
		parts := strings.Split(data, "|")
		if len(parts) >= 4 && strings.HasPrefix(parts[2], "gid:") && strings.HasPrefix(parts[3], "p:") {
			eid := mustAtoi64(strings.TrimPrefix(parts[0], "expdel|"))
			gid := mustAtoi64(strings.TrimPrefix(parts[2], "gid:"))
			page := mustAtoi(strings.TrimPrefix(parts[3], "p:"))
			if err := a.repo.deleteExpense(eid); err == nil {
				_, _ = ctx.CallbackQuery.Answer(b, &tgb.AnswerCallbackQueryOpts{Text: "Удалено"})
				_ = a.sendExpensesPage(b, ctx, gid, page)
				return nil
			}
		}
		_, _ = ctx.CallbackQuery.Answer(b, &tgb.AnswerCallbackQueryOpts{Text: "Ошибка удаления"})
		return nil

	// confirm payment
	case strings.HasPrefix(data, "pay|"):
		// pay|gid:<gid>|to:<uid>|amt:<cents>
		parts := strings.Split(data, "|")
		if len(parts) == 4 && strings.HasPrefix(parts[1], "gid:") && strings.HasPrefix(parts[2], "to:") && strings.HasPrefix(parts[3], "amt:") {
			gid := mustAtoi64(strings.TrimPrefix(parts[1], "gid:"))
			to := mustAtoi64(strings.TrimPrefix(parts[2], "to:"))
			amt := mustAtoi64(strings.TrimPrefix(parts[3], "amt:"))
			_, err := a.db.Exec(`INSERT INTO settlements(group_id,from_tg_id,to_tg_id,amount_cents,confirmed_by_to,created_at)
				VALUES(?,?,?,?,?,?)`, gid, uid, to, amt, 1, nowUnix())
			if err == nil {
				_, _ = ctx.CallbackQuery.Answer(b, &tgb.AnswerCallbackQueryOpts{Text: "Оплата подтверждена"})
				_ = a.sendGroupDetails(b, ctx, gid)
				return nil
			}
		}
		_, _ = ctx.CallbackQuery.Answer(b, &tgb.AnswerCallbackQueryOpts{Text: "Ошибка подтверждения"})
		return nil
	}

	// add-expense flow callbacks
	st, ok := a.state.Get(uid)
	if !ok {
		_, _ = ctx.CallbackQuery.Answer(b, nil)
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
		_, _ = ctx.CallbackQuery.Answer(b, nil)
		return a.askParticipants(b, ctx, st)
	case strings.HasPrefix(data, "part_done"):
		st.Step = "choose_split"
		a.state.Set(uid, st)
		_, _ = ctx.CallbackQuery.Answer(b, nil)
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

	_, _ = ctx.CallbackQuery.Answer(b, nil)
	return nil
}

func mustAtoi(s string) int {
	i, _ := strconv.Atoi(s)
	return i
}
func mustAtoi64(s string) int64 {
	i, _ := strconv.ParseInt(s, 10, 64)
	return i
}

// ---------- Add expense sub-steps ----------

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
	rows = append(rows, []tgb.InlineKeyboardButton{{Text: "Готово →", CallbackData: "part_done"}})
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
	_, _ = ctx.EffectiveChat.SendMessage(b, fmt.Sprintf("Введите сумму для участника %s (остаток — %s):",
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

// ---------- Other commands ----------

func (a *app) onBalances(b *tgb.Bot, ctx *ext.Context) error {
	uid := ctx.EffectiveUser.Id
	gs, err := a.repo.listUserGroups(uid)
	if err != nil {
		return err
	}
	if len(gs) == 0 {
		_, _ = ctx.EffectiveChat.SendMessage(b, "У вас нет групп. Создайте: /newgroup <название>", nil)
		return nil
	}
	var sb strings.Builder
	sb.WriteString("Ваши долги по группам:\n")
	for _, g := range gs {
		bal, err := a.repo.computeGroupBalances(g.ID)
		if err != nil {
			return err
		}
		count := 0
		for k, v := range bal {
			if v > 0 && k.From == uid {
				if count == 0 {
					sb.WriteString(fmt.Sprintf("— #%d %s\n", g.ID, g.Title))
				}
				sb.WriteString(fmt.Sprintf("   вы → %s: %s\n", a.repo.userName(k.To), formatCents(v)))
				count++
			}
		}
		if count == 0 {
			sb.WriteString(fmt.Sprintf("— #%d %s: долгов нет\n", g.ID, g.Title))
		}
	}
	_, _ = ctx.EffectiveChat.SendMessage(b, sb.String(), nil)
	return nil
}

// Новое: /join <group_id>
func (a *app) onJoin(b *tgb.Bot, ctx *ext.Context) error {
	if len(ctx.Args()) != 1 {
		_, _ = ctx.EffectiveChat.SendMessage(b, "Использование: /join <group_id>", nil)
		return nil
	}
	gid, err := strconv.ParseInt(ctx.Args()[0], 10, 64)
	if err != nil {
		_, _ = ctx.EffectiveChat.SendMessage(b, "Некорректный group_id", nil)
		return nil
	}
	if title, err := a.repo.joinByGroupID(gid, ctx.EffectiveUser.Id); err == nil {
		_, _ = ctx.EffectiveChat.SendMessage(b, fmt.Sprintf("Вы присоединились к группе #%d: %s", gid, title), nil)
		return nil
	} else {
		return err
	}
}
