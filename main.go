package main

// Expense-splitting Telegram bot (gotgbot v2 + SQLite, pure Go).
// Новое:
// - Все колбэки редактируют исходное сообщение (EditMessageText).
// - Инвайт по коду: https://t.me/<bot>?start=<invite_code>; /join <code>; /join_<code>.
// - Клавиатура с кнопками вместо команд.
// - В деталях группы: "Вы должны" и "Вам должны" + подтверждение оплаты.
// - Кнопка "Взаимозачёт" (по всем группам).
// - Исправлено: нельзя создать группу без названия; удаление групп; удаление трат.
// - Пагинации через edit.
// - Фикс зависания при вводе сумм в "кастомных долях": корректно обрабатываем отсутствие клавиатуры.
// - /cancel для отмены текущего мастера.
// - Кнопка «Поделиться /join…» через tg://msg?text= (без @username), с заменой '+' -> '%20'.
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
	"net/url"
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

// ---------- small helpers ----------

func sp(s string) *string { return &s } // string pointer helper

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
PRAGMA foreign_keys=ON;

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
	mu            sync.RWMutex
	addEx         map[int64]*addExpenseState
	newGroupAsk   map[int64]bool
	lastMsgByUser map[int64]int
}

func newStateStore() *stateStore {
	return &stateStore{
		addEx:         map[int64]*addExpenseState{},
		newGroupAsk:   map[int64]bool{},
		lastMsgByUser: map[int64]int{},
	}
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
func (s *stateStore) SetNewGroup(uid int64, v bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.newGroupAsk[uid] = v
}
func (s *stateStore) IsNewGroup(uid int64) bool {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return s.newGroupAsk[uid]
}
func (s *stateStore) SetLastMsg(uid int64, mid int) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.lastMsgByUser[uid] = mid
}
func (s *stateStore) LastMsg(uid int64) (int, bool) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	m, ok := s.lastMsgByUser[uid]
	return m, ok
}

// ---------- Repo ----------

type repo struct{ db *sql.DB }

func (r *repo) upsertUser(tgID int64, name string) error {
	_, err := r.db.Exec(`INSERT INTO users(tg_id,name) VALUES(?,?)
		ON CONFLICT(tg_id) DO UPDATE SET name=excluded.name`, tgID, name)
	return err
}

func (r *repo) createGroup(title string, owner int64) (int64, string, error) {
	code, err := randCode(8)
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

func (r *repo) isGroupOwner(groupID, uid int64) (bool, error) {
	var owner int64
	if err := r.db.QueryRow(`SELECT owner_tg_id FROM groups WHERE id=?`, groupID).Scan(&owner); err != nil {
		return false, err
	}
	return owner == uid, nil
}

func (r *repo) deleteGroup(groupID int64) error {
	_, err := r.db.Exec(`DELETE FROM groups WHERE id=?`, groupID)
	return err
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
		for _, on := range st.Participants {
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
	// 1) Собираем все траты
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

	// Доли по тратам
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

	// 2) Считаем «кто кому должен» из трат
	bal := map[pair]int64{}
	for _, e := range exps {
		for uid, s := range sharesByExp[e.id] {
			if uid == e.payer {
				continue
			}
			bal[pair{From: uid, To: e.payer}] += s
		}
	}

	// 3) Применяем подтверждённые оплаты (settlements)
	//    Оплата уменьшает долг From -> To. Если оплаты больше, остаток
	//    разворачивается как долг в обратную сторону.
	setRows, err := r.db.Query(`
SELECT from_tg_id, to_tg_id, amount_cents
FROM settlements
WHERE group_id=? AND confirmed_by_to=1
`, groupID)
	if err != nil {
		return nil, err
	}
	defer setRows.Close()

	for setRows.Next() {
		var from, to, amt int64
		if err := setRows.Scan(&from, &to, &amt); err != nil {
			return nil, err
		}
		k := pair{From: from, To: to}
		cur := bal[k]
		if cur >= amt {
			bal[k] = cur - amt
			if bal[k] == 0 {
				delete(bal, k)
			}
		} else {
			over := amt - cur
			delete(bal, k)
			// Переплата превращается в долг получателя перед плательщиком
			if over > 0 {
				bal[pair{From: to, To: from}] += over
			}
		}
	}

	// 4) Финальное взаимозачётное схлопывание пар
	for k := range bal {
		inv := pair{From: k.To, To: k.From}
		if v2, ok := bal[inv]; ok {
			if bal[k] >= v2 {
				bal[k] = bal[k] - v2
				delete(bal, inv)
				if bal[k] == 0 {
					delete(bal, k)
				}
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

	// Команды (совместимость) + cancel
	dispatcher.AddHandler(handlers.NewCommand("start", a.onStart))
	dispatcher.AddHandler(handlers.NewCommand("join", a.onJoin))
	dispatcher.AddHandler(handlers.NewCommand("cancel", a.onCancel))

	dispatcher.AddHandler(handlers.NewCallback(callbackquery.All, a.cb))
	dispatcher.AddHandler(handlers.NewMessage(message.Text, a.onText))

	log.Printf("Starting bot @%s ...", me.Username)
	if err := updater.StartPolling(bot, &ext.PollingOpts{DropPendingUpdates: true}); err != nil {
		log.Fatalf("start polling: %v", err)
	}
	updater.Idle()
}

// ---------- Keyboard ----------

func mainKeyboard() *tgb.ReplyKeyboardMarkup {
	return &tgb.ReplyKeyboardMarkup{
		Keyboard: [][]tgb.KeyboardButton{
			{{Text: "➕ Создать группу"}, {Text: "👥 Мои группы"}},
			{{Text: "🔗 Приглашение"}, {Text: "🧾 Добавить трату"}},
			{{Text: "📊 Балансы"}, {Text: "🔄 Взаимозачёт"}},
		},
		ResizeKeyboard:  true,
		OneTimeKeyboard: false,
	}
}

// ---------- Handlers ----------

func (a *app) onStart(b *tgb.Bot, ctx *ext.Context) error {
    uid := ctx.EffectiveUser.Id
    name := getBestName(ctx)
    _ = a.repo.upsertUser(uid, name)

    // --- NEW: accept /start@<code> and /start_<code> too ---
    raw := strings.TrimSpace(ctx.EffectiveMessage.Text)
    // normalize weird spaces
    raw = strings.ReplaceAll(raw, "\u00A0", " ")
    raw = strings.ReplaceAll(raw, "\u2009", " ")
    raw = strings.ReplaceAll(raw, "\u202F", " ")

    var code string
    if args := ctx.Args(); len(args) == 1 {
        code = args[0]
    } else if strings.HasPrefix(raw, "/start@") {
        code = strings.TrimSpace(strings.TrimPrefix(raw, "/start@"))
    } else if strings.HasPrefix(raw, "/start_") {
        code = strings.TrimSpace(strings.TrimPrefix(raw, "/start_"))
    }
    if code != "" {
        if gid, title, err := a.repo.joinByCode(code, uid); err == nil {
            _, _ = ctx.EffectiveChat.SendMessage(
                b,
                fmt.Sprintf("Вы присоединились к группе #%d: %s", gid, title),
                &tgb.SendMessageOpts{ReplyMarkup: mainKeyboard()},
            )
            return nil
        }
        // fall through to greeting if code invalid
    }
    // --------------------------------------------------------

    msg := "Привет! Я помогу делить траты в поездках.\nИспользуйте кнопки ниже."
    _, _ = ctx.EffectiveChat.SendMessage(b, msg, &tgb.SendMessageOpts{ReplyMarkup: mainKeyboard()})
    return nil
}


func (a *app) onCancel(b *tgb.Bot, ctx *ext.Context) error {
	uid := ctx.EffectiveUser.Id
	if _, ok := a.state.Get(uid); ok || a.state.IsNewGroup(uid) {
		a.state.Del(uid)
		a.state.SetNewGroup(uid, false)
		_, _ = ctx.EffectiveChat.SendMessage(b, "Ок, отменил. Можно начать заново.", &tgb.SendMessageOpts{ReplyMarkup: mainKeyboard()})
		return nil
	}
	_, _ = ctx.EffectiveChat.SendMessage(b, "Нечего отменять.", nil)
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

// ---------- Pagination helpers ----------

const groupsPerPage = 5
const expensesPerPage = 10

func (a *app) buildGroupsPageKeyboard(uid int64, page int, mode string) (*tgb.InlineKeyboardMarkup, int, error) {
	gs, err := a.repo.listUserGroups(uid)
	if err != nil {
		return nil, 0, err
	}
	total := len(gs)
	if total == 0 {
		return &tgb.InlineKeyboardMarkup{InlineKeyboard: [][]tgb.InlineKeyboardButton{}}, 0, nil
	}
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

// ---------- Edit helpers (фикс typed-nil) ----------

func editOrSend(b *tgb.Bot, ctx *ext.Context, text string, markup *tgb.InlineKeyboardMarkup) {
	// Если это ответ на callback — редактируем исходное сообщение
	if ctx.CallbackQuery != nil && ctx.CallbackQuery.Message != nil {
		if markup != nil {
			_, _, _ = ctx.CallbackQuery.Message.EditText(b, text, &tgb.EditMessageTextOpts{
				ReplyMarkup: *markup, // значение
			})
		} else {
			_, _, _ = ctx.CallbackQuery.Message.EditText(b, text, nil)
		}
		return
	}

	// Обычная отправка: НЕ передавать typed-nil в интерфейс ReplyMarkup.
	if markup != nil {
		_, _ = ctx.EffectiveChat.SendMessage(b, text, &tgb.SendMessageOpts{ReplyMarkup: markup})
	} else {
		_, _ = ctx.EffectiveChat.SendMessage(b, text, nil)
	}
}

// ---------- Top-level buttons ----------

func (a *app) onText(b *tgb.Bot, ctx *ext.Context) error {
	txt := strings.TrimSpace(ctx.EffectiveMessage.Text)

	uid := ctx.EffectiveUser.Id

	if a.state.IsNewGroup(uid) || func() bool { _, ok := a.state.Get(uid); return ok }() {
		// If the user hits any top-level button during a wizard – don’t start it.
		top := map[string]bool{
			"➕ Создать группу": true, "👥 Мои группы": true, "🔗 Приглашение": true,
			"🧾 Добавить трату": true, "📊 Балансы": true, "🔄 Взаимозачёт": true,
		}
		if top[txt] {
			_, _ = ctx.EffectiveChat.SendMessage(b, "Сейчас идёт мастер. Отправьте запрошенные данные или нажмите /cancel (или «❌ Отмена»).", nil)
			return nil
		}
	}

	// quick accept: /start@<code> or /start_<code>
	if strings.HasPrefix(txt, "/start@") || strings.HasPrefix(txt, "/start_") {
		code := strings.TrimPrefix(strings.TrimPrefix(txt, "/start@"), "/start_")
		code = strings.TrimSpace(code)
		if gid, title, err := a.repo.joinByCode(code, ctx.EffectiveUser.Id); err == nil {
			_, _ = ctx.EffectiveChat.SendMessage(b,
				fmt.Sprintf("Вы присоединились к группе #%d: %s", gid, title),
				&tgb.SendMessageOpts{ReplyMarkup: mainKeyboard()},
			)
			return nil
		}
	}

	// быстрый парсер команд вида /join_<code>
	if strings.HasPrefix(txt, "/join_") {
		fields := strings.Fields(strings.TrimPrefix(txt, "/join_"))
		if len(fields) > 0 {
			code := fields[0]
			if gid, title, err := a.repo.joinByCode(code, ctx.EffectiveUser.Id); err == nil {
				_, _ = ctx.EffectiveChat.SendMessage(b, fmt.Sprintf("Вы присоединились к группе #%d: %s", gid, title),
					&tgb.SendMessageOpts{ReplyMarkup: mainKeyboard()})
				return nil
			}
		}
	}

	// если ждём название новой группы
	if a.state.IsNewGroup(uid) {
		title := strings.TrimSpace(txt)
		if title == "" || strings.HasPrefix(title, "/") {
			_, _ = ctx.EffectiveChat.SendMessage(b, "Название не может быть пустым. Введите название группы одним сообщением.", nil)
			return nil
		}
		a.state.SetNewGroup(uid, false)
		gid, code, err := a.repo.createGroup(title, uid)
		if err != nil {
			return err
		}
		link := fmt.Sprintf("https://t.me/%s?start=%s", a.base, code)
		_, _ = ctx.EffectiveChat.SendMessage(b, fmt.Sprintf(
			"Группа #%d создана: %s\nПриглашение: %s\nКоманда: /join %s",
			gid, title, link, code),
			&tgb.SendMessageOpts{ReplyMarkup: mainKeyboard()})
		return nil
	}

	// если находимся в мастере добавления траты
	if st, ok := a.state.Get(uid); ok {
		return a.onTextFlowAddExpense(b, ctx, st, txt)
	}

	// обработка кнопок
	switch txt {
	case "➕ Создать группу":
		a.state.SetNewGroup(uid, true)
		_, _ = ctx.EffectiveChat.SendMessage(b, "Введи название новой группы одним сообщением.", nil)
		return nil
	case "👥 Мои группы":
		markup, total, err := a.buildGroupsPageKeyboard(uid, 0, "mg")
		if err != nil {
			return err
		}
		if total == 0 {
			_, _ = ctx.EffectiveChat.SendMessage(b, "У вас нет групп. Нажмите «Создать группу».", nil)
			return nil
		}
		editOrSend(b, ctx, "Выберите группу:", markup)
		return nil
	case "🔗 Приглашение":
		markup, total, err := a.buildGroupsPageKeyboard(uid, 0, "inv")
		if err != nil {
			return err
		}
		if total == 0 {
			_, _ = ctx.EffectiveChat.SendMessage(b, "У вас нет групп. Нажмите «Создать группу».", nil)
			return nil
		}
		editOrSend(b, ctx, "Выберите группу для приглашения:", markup)
		return nil
	case "🧾 Добавить трату":
		markup, total, err := a.buildGroupsPageKeyboard(uid, 0, "ae")
		if err != nil {
			return err
		}
		if total == 0 {
			_, _ = ctx.EffectiveChat.SendMessage(b, "У вас нет групп. Нажмите «Создать группу».", nil)
			return nil
		}
		editOrSend(b, ctx, "Выберите группу для добавления траты:", markup)
		return nil
	case "📊 Балансы":
		return a.showBalancesAll(b, ctx)
	case "🔄 Взаимозачёт":
		return a.showCrossNet(b, ctx)
	}

	return nil
}

func (a *app) onTextFlowAddExpense(b *tgb.Bot, ctx *ext.Context, st *addExpenseState, txt string) error {
	uid := ctx.EffectiveUser.Id

	switch st.Step {
	case "await_amount_desc":
		parts := strings.Fields(txt)
		if len(parts) == 0 {
			_, _ = ctx.EffectiveChat.SendMessage(b, "Нужно прислать сумму и описание. Пример: 1200 обед", inlineCancel())
			return nil
		}
		amt, err := centsFromStr(parts[0])
		if err != nil || amt <= 0 {
			_, _ = ctx.EffectiveChat.SendMessage(b, "Сумма не распознана. Пример: 1200 обед", inlineCancel())
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
		if st.CustomShares == nil {
			st.CustomShares = map[int64]int64{}
		}
		if len(st.CustomLeft) == 0 {
			st.Step = "confirm"
			a.state.Set(uid, st)
			return a.finalizeExpense(b, ctx, st)
		}

		amt, err := centsFromStr(strings.TrimSpace(txt))
		if err != nil || amt < 0 {
			editOrSend(b, ctx, "Сумма не распознана. Пришлите число, напр. 350.50", inlineCancel())
			return nil
		}

		nextUID := st.CustomLeft[0]
		name := a.repo.userName(nextUID)
		remaining := st.AmountCents - sumMap(st.CustomShares)

		if amt > remaining {
			editOrSend(b, ctx,
				fmt.Sprintf("Слишком много. Остаток — %s. Введите сумму для %s не больше остатка.",
					formatCents(remaining), name), inlineCancel()
				)
			return nil
		}
		// Для последнего участника — автодобивание остатка.
		if len(st.CustomLeft) == 1 && amt != remaining {
			amt = remaining
		}

		st.CustomShares[nextUID] = amt
		st.CustomLeft = st.CustomLeft[1:]
		a.state.Set(uid, st)

		var parts []string
		for pid, v := range st.CustomShares {
			parts = append(parts, fmt.Sprintf("%s: %s", a.repo.userName(pid), formatCents(v)))
		}
		sort.Strings(parts)
		progress := "Назначено:\n• " + strings.Join(parts, "\n• ")

		if len(st.CustomLeft) == 0 {
			editOrSend(b, ctx, progress+"\nВсе суммы заданы. Сохраняю…", inlineCancel()
		)
			return a.finalizeExpense(b, ctx, st)
		}
		next := st.CustomLeft[0]
		editOrSend(b, ctx, fmt.Sprintf("%s\n\nВведите сумму для участника %s (остаток — %s):",
			progress, a.repo.userName(next), formatCents(st.AmountCents-sumMap(st.CustomShares))), inlineCancel()
		)
		return nil
	}

	return nil
}

func (a *app) askNextCustom(b *tgb.Bot, ctx *ext.Context, st *addExpenseState) error {
	if len(st.CustomLeft) == 0 {
		return a.finalizeExpense(b, ctx, st)
	}
	if st.CustomShares == nil {
		st.CustomShares = map[int64]int64{}
	}
	uid := st.CustomLeft[0]
	name := a.repo.userName(uid)
	remaining := st.AmountCents - sumMap(st.CustomShares)

	var parts []string
	for pid, v := range st.CustomShares {
		parts = append(parts, fmt.Sprintf("%s: %s", a.repo.userName(pid), formatCents(v)))
	}
	sort.Strings(parts)
	progress := ""
	if len(parts) > 0 {
		progress = "Назначено:\n• " + strings.Join(parts, "\n• ") + "\n\n"
	}
	editOrSend(b, ctx,
		fmt.Sprintf("%sВведите сумму для участника %s (остаток — %s, максимум — %s):",
			progress, name, formatCents(remaining), formatCents(remaining)),
		inlineCancel()
	)
	return nil
}

// ---------- Callback router (only edits) ----------

func (a *app) cb(b *tgb.Bot, ctx *ext.Context) error {
	data := ctx.CallbackQuery.Data
	uid := ctx.EffectiveUser.Id

	switch {
		
	case data == "cancel_flow":
        a.state.Del(uid)            // drop add-expense state if any
        a.state.SetNewGroup(uid, false)
        editOrSend(b, ctx, "Отменено. Что дальше?", nil)
        _, _ = ctx.CallbackQuery.Answer(b, &tgb.AnswerCallbackQueryOpts{Text: "Отменено"})
        return nil
	// paging: groups
	case strings.HasPrefix(data, "mg|p:"):
		page := mustAtoi(strings.TrimPrefix(data, "mg|p:"))
		markup, _, err := a.buildGroupsPageKeyboard(uid, page, "mg")
		if err == nil {
			editOrSend(b, ctx, "Выберите группу:", markup)
		}
		_, _ = ctx.CallbackQuery.Answer(b, nil)
		return nil
	case strings.HasPrefix(data, "inv|p:"):
		page := mustAtoi(strings.TrimPrefix(data, "inv|p:"))
		markup, _, err := a.buildGroupsPageKeyboard(uid, page, "inv")
		if err == nil {
			editOrSend(b, ctx, "Выберите группу для приглашения:", markup)
		}
		_, _ = ctx.CallbackQuery.Answer(b, nil)
		return nil
	case strings.HasPrefix(data, "ae|p:"):
		page := mustAtoi(strings.TrimPrefix(data, "ae|p:"))
		markup, _, err := a.buildGroupsPageKeyboard(uid, page, "ae")
		if err == nil {
			editOrSend(b, ctx, "Выберите группу для добавления траты:", markup)
		}
		_, _ = ctx.CallbackQuery.Answer(b, nil)
		return nil

	// selections
	case strings.HasPrefix(data, "mgsel|"):
		gid := mustAtoi64(strings.TrimPrefix(data, "mgsel|"))
		_ = a.sendGroupDetailsEdit(b, ctx, gid)
		_, _ = ctx.CallbackQuery.Answer(b, nil)
		return nil
	case strings.HasPrefix(data, "invsel|"):
		gid := mustAtoi64(strings.TrimPrefix(data, "invsel|"))
		_ = a.sendInviteForGroupEdit(b, ctx, gid)
		_, _ = ctx.CallbackQuery.Answer(b, &tgb.AnswerCallbackQueryOpts{Text: "Выберите чат для отправки"})
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
			_ = a.sendExpensesPageEdit(b, ctx, gid, page)
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
				_ = a.sendExpensesPageEdit(b, ctx, gid, page)
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
				_ = a.sendGroupDetailsEdit(b, ctx, gid)
				return nil
			}
		}
		_, _ = ctx.CallbackQuery.Answer(b, &tgb.AnswerCallbackQueryOpts{Text: "Ошибка подтверждения"})
		return nil

	// delete group (owner)
	case strings.HasPrefix(data, "grpdel|"):
		gid := mustAtoi64(strings.TrimPrefix(data, "grpdel|gid:"))
		uid := ctx.EffectiveUser.Id
		if isOwner, _ := a.repo.isGroupOwner(gid, uid); !isOwner {
			_, _ = ctx.CallbackQuery.Answer(b, &tgb.AnswerCallbackQueryOpts{Text: "Только владелец может удалить группу"})
			return nil
		}
		text := fmt.Sprintf("Точно удалить группу #%d? Это удалит все её данные.", gid)
		markup := &tgb.InlineKeyboardMarkup{InlineKeyboard: [][]tgb.InlineKeyboardButton{
			{{Text: "Да, удалить", CallbackData: fmt.Sprintf("grpdelyes|gid:%d", gid)}},
			{{Text: "Отмена", CallbackData: fmt.Sprintf("mgsel|%d", gid)}},
		}}
		editOrSend(b, ctx, text, markup)
		_, _ = ctx.CallbackQuery.Answer(b, nil)
		return nil

	case strings.HasPrefix(data, "grpdelyes|"):
		gid := mustAtoi64(strings.TrimPrefix(data, "grpdelyes|gid:"))
		if err := a.repo.deleteGroup(gid); err == nil {
			_, _ = ctx.CallbackQuery.Answer(b, &tgb.AnswerCallbackQueryOpts{Text: "Группа удалена"})
			markup, _, _ := a.buildGroupsPageKeyboard(uid, 0, "mg")
			editOrSend(b, ctx, "Группа удалена. Ваши группы:", markup)
			return nil
		}
		_, _ = ctx.CallbackQuery.Answer(b, &tgb.AnswerCallbackQueryOpts{Text: "Ошибка удаления группы"})
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

// ---------- Screens (EDIT variants) ----------

func (a *app) sendGroupDetailsEdit(b *tgb.Bot, ctx *ext.Context, gid int64) error {
	uid := ctx.EffectiveUser.Id

	code, err := a.repo.getInviteCode(gid)
	if err != nil {
		return err
	}
	link := fmt.Sprintf("https://t.me/%s?start=%s", a.base, code)

	bal, err := a.repo.computeGroupBalances(gid)
	if err != nil {
		return err
	}

	var youOwe, oweYou []string
	for k, v := range bal {
		if v <= 0 {
			continue
		}
		switch {
		case k.From == uid:
			youOwe = append(youOwe, fmt.Sprintf("вы → %s: %s", a.repo.userName(k.To), formatCents(v)))
		case k.To == uid:
			oweYou = append(oweYou, fmt.Sprintf("%s → вам: %s", a.repo.userName(k.From), formatCents(v)))
		}
	}
	sort.Strings(youOwe)
	sort.Strings(oweYou)

	var lines []string
	lines = append(lines, fmt.Sprintf("Группа #%d", gid))
	lines = append(lines, fmt.Sprintf("Приглашение: %s\nКоманда: /join %s", link, code))
	if len(youOwe) == 0 && len(oweYou) == 0 {
		lines = append(lines, "В этой группе долгов нет 🎉")
	} else {
		if len(youOwe) > 0 {
			lines = append(lines, "Вы должны:")
			lines = append(lines, "• "+strings.Join(youOwe, "\n• "))
		}
		if len(oweYou) > 0 {
			lines = append(lines, "Вам должны:")
			lines = append(lines, "• "+strings.Join(oweYou, "\n• "))
		}
	}

	// Кнопка «Поделиться /join…» — открывает выбор чата с правильным пробелом.
	txt := "/join " + code
	enc := url.QueryEscape(txt)
	enc = strings.ReplaceAll(enc, "+", "%20")
	share := fmt.Sprintf("tg://msg?text=%s", enc)

	rows := [][]tgb.InlineKeyboardButton{
		{{Text: "Поделиться /join…", Url: share}},
		{{Text: "Список трат", CallbackData: fmt.Sprintf("explist|%d|p:%d", gid, 0)}},
	}
	if isOwner, _ := a.repo.isGroupOwner(gid, uid); isOwner {
		rows = append(rows, []tgb.InlineKeyboardButton{
			{Text: "🗑 Удалить группу", CallbackData: fmt.Sprintf("grpdel|gid:%d", gid)},
		})
	}
	rows = append(rows, []tgb.InlineKeyboardButton{{Text: "Назад к моим группам", CallbackData: "mg|p:0"}})

	// индивидуальные подтверждения
	for k, v := range bal {
		if v > 0 && k.From == uid {
			btn := tgb.InlineKeyboardButton{
				Text:         fmt.Sprintf("Подтвердить оплату → %s (%s)", a.repo.userName(k.To), formatCents(v)),
				CallbackData: fmt.Sprintf("pay|gid:%d|to:%d|amt:%d", gid, k.To, v),
			}
			rows = append([][]tgb.InlineKeyboardButton{{btn}}, rows...)
		}
	}

	editOrSend(b, ctx, strings.Join(lines, "\n"), &tgb.InlineKeyboardMarkup{InlineKeyboard: rows})
	return nil
}

func (a *app) sendInviteForGroupEdit(b *tgb.Bot, ctx *ext.Context, gid int64) error {
	code, err := a.repo.getInviteCode(gid)
	if err != nil {
		return err
	}
	urlStart := fmt.Sprintf("https://t.me/%s?start=%s", a.base, code)

	txt := "/join " + code
	enc := url.QueryEscape(txt)
	enc = strings.ReplaceAll(enc, "+", "%20")
	share := fmt.Sprintf("tg://msg?text=%s", enc)

	text := fmt.Sprintf("Приглашение в группу #%d:\n%s\nКоманда: /join %s", gid, urlStart, code)
	editOrSend(b, ctx, text, &tgb.InlineKeyboardMarkup{InlineKeyboard: [][]tgb.InlineKeyboardButton{
		{{Text: "Поделиться /join…", Url: share}},
	}})
	return nil
}

func (a *app) sendExpensesPageEdit(b *tgb.Bot, ctx *ext.Context, gid int64, page int) error {
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
		editOrSend(b, ctx, "В группе пока нет трат.",
			&tgb.InlineKeyboardMarkup{InlineKeyboard: [][]tgb.InlineKeyboardButton{
				{{Text: "Назад к группе", CallbackData: fmt.Sprintf("mgsel|%d", gid)}},
			}})
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
	nav := []tgb.InlineKeyboardButton{{Text: "Назад к группе", CallbackData: fmt.Sprintf("mgsel|%d", gid)}}
	if page > 0 {
		nav = append([]tgb.InlineKeyboardButton{{Text: "« Назад", CallbackData: fmt.Sprintf("explist|%d|p:%d", gid, page-1)}}, nav...)
	}
	if offset+expensesPerPage < total {
		nav = append(nav, tgb.InlineKeyboardButton{Text: "Вперёд »", CallbackData: fmt.Sprintf("explist|%d|p:%d", gid, page+1)})
	}
	rows = append(rows, nav)

	editOrSend(b, ctx, sb.String(), &tgb.InlineKeyboardMarkup{InlineKeyboard: rows})
	return nil
}

// ---------- Add expense sub-steps ----------

func (a *app) askPayer(b *tgb.Bot, ctx *ext.Context, st *addExpenseState) error {
	members, err := a.repo.listMembers(st.GroupID)
	if err != nil || len(members) == 0 {
		editOrSend(b, ctx, "В группе пока нет участников.", nil)
		return nil
	}
	var btns [][]tgb.InlineKeyboardButton
	for _, m := range members {
		btns = append(btns, []tgb.InlineKeyboardButton{
			{Text: fmt.Sprintf("Плательщик: %s", m.Name), CallbackData: fmt.Sprintf("payer|%d", m.ID)},
		})
	}
	btns = append(btns, []tgb.InlineKeyboardButton{{Text: "❌ Отмена", CallbackData: "cancel_flow"}})
	text := fmt.Sprintf("Сумма: %s\nОписание: %s\nВыберите плательщика:", formatCents(st.AmountCents), st.Description)
	editOrSend(b, ctx, text, &tgb.InlineKeyboardMarkup{InlineKeyboard: btns})
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
			label = "❌ " + label   // was "☑️ "
		}
		rows = append(rows, []tgb.InlineKeyboardButton{
			{Text: label, CallbackData: fmt.Sprintf("toggle|%d", m.ID)},
		})
	}	
	rows = append(rows, []tgb.InlineKeyboardButton{{Text: "Готово →", CallbackData: "part_done"}})
	rows = append(rows, []tgb.InlineKeyboardButton{{Text: "❌ Отмена", CallbackData: "cancel_flow"}})
	editOrSend(b, ctx, "Выберите участников (нажимайте, чтобы включить/исключить), затем «Готово».",
		&tgb.InlineKeyboardMarkup{InlineKeyboard: rows})
	return nil
}

func (a *app) askSplitMode(b *tgb.Bot, ctx *ext.Context, st *addExpenseState) error {
	btns := [][]tgb.InlineKeyboardButton{
		{{Text: "Поровну", CallbackData: "split|equal"}},
		{{Text: "Свои доли", CallbackData: "split|custom"}},
	}
	editOrSend(b, ctx, "Как разделить?", &tgb.InlineKeyboardMarkup{InlineKeyboard: btns})
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
			editOrSend(b, ctx, "Сумма долей не равна общей сумме. Нажмите /cancel и начните заново.", nil)
			return nil
		}
	}
	id, err := a.repo.createExpenseTx(context.Background(), st)
	if err != nil {
		return err
	}
	a.state.Del(ctx.EffectiveUser.Id)
	editOrSend(b, ctx, fmt.Sprintf("Трата #%d добавлена. Сумма %s, плательщик %s.", id, formatCents(st.AmountCents), a.repo.userName(st.Payer)), nil)
	return nil
}

// ---------- Balances / Cross-net ----------

func (a *app) showBalancesAll(b *tgb.Bot, ctx *ext.Context) error {
	uid := ctx.EffectiveUser.Id
	gs, err := a.repo.listUserGroups(uid)
	if err != nil {
		return err
	}
	if len(gs) == 0 {
		_, _ = ctx.EffectiveChat.SendMessage(b, "У вас нет групп. Нажмите «Создать группу».", nil)
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

func (a *app) showCrossNet(b *tgb.Bot, ctx *ext.Context) error {
	uid := ctx.EffectiveUser.Id
	all, err := a.repo.computeCrossGroupNet(uid)
	if err != nil {
		return err
	}
	if len(all) == 0 {
		_, _ = ctx.EffectiveChat.SendMessage(b, "По всем группам взаимозачёт = 0. Никто никому не должен ✨", nil)
		return nil
	}
	var youOwe, oweYou []string
	for k, v := range all {
		if v <= 0 {
			continue
		}
		if k.From == uid {
			youOwe = append(youOwe, fmt.Sprintf("вы → %s: %s", a.repo.userName(k.To), formatCents(v)))
		} else if k.To == uid {
			oweYou = append(oweYou, fmt.Sprintf("%s → вам: %s", a.repo.userName(k.From), formatCents(v)))
		}
	}
	sort.Strings(youOwe)
	sort.Strings(oweYou)

	var sb strings.Builder
	sb.WriteString("Взаимозачёт по всем группам:\n")
	if len(youOwe) == 0 && len(oweYou) == 0 {
		sb.WriteString("Никто никому не должен 🎉")
	} else {
		if len(youOwe) > 0 {
			sb.WriteString("Вы должны:\n• " + strings.Join(youOwe, "\n• ") + "\n")
		}
		if len(oweYou) > 0 {
			sb.WriteString("Вам должны:\n• " + strings.Join(oweYou, "\n• "))
		}
	}
	_, _ = ctx.EffectiveChat.SendMessage(b, sb.String(), nil)
	return nil
}

// ---------- Compatibility /join <code> ----------

func (a *app) onJoin(b *tgb.Bot, ctx *ext.Context) error {
	raw := ctx.EffectiveMessage.Text

	// Нормализуем: убираем имя бота, плюсы и разные неразрывные пробелы.
	raw = strings.ReplaceAll(raw, "\u00A0", " ") // NBSP
	raw = strings.ReplaceAll(raw, "\u2009", " ") // thin space
	raw = strings.ReplaceAll(raw, "\u202F", " ") // narrow NBSP
	raw = strings.ReplaceAll(raw, "+", " ")
	raw = strings.TrimSpace(raw)

	// Поддержка /join и /join@<bot>
	raw = strings.TrimPrefix(raw, "/join@"+a.base)
	raw = strings.TrimPrefix(raw, "/join")
	raw = strings.TrimSpace(raw)

	code := ""
	switch {
	case strings.HasPrefix(raw, "_"): // /join_<code>
		code = strings.TrimPrefix(raw, "_")
	default:
		if f := strings.Fields(raw); len(f) > 0 {
			code = f[0]
		}
	}

	if code == "" {
		_, _ = ctx.EffectiveChat.SendMessage(b, "Использование: /join <код>\nТакже работает команда: /join_<код> и ссылка /start <код>", nil)
		return nil
	}

	if gid, title, err := a.repo.joinByCode(code, ctx.EffectiveUser.Id); err == nil {
		_, _ = ctx.EffectiveChat.SendMessage(b, fmt.Sprintf("Вы присоединились к группе #%d: %s", gid, title),
			&tgb.SendMessageOpts{ReplyMarkup: mainKeyboard()})
		return nil
	} else {
		return err
	}
}

func inlineCancel() *tgb.InlineKeyboardMarkup {
    return &tgb.InlineKeyboardMarkup{InlineKeyboard: [][]tgb.InlineKeyboardButton{
        {{Text: "❌ Отмена", CallbackData: "cancel_flow"}},
    }}
}
