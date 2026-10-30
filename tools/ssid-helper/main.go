// SSID helper observes only the browser profile it creates for manual login.
package main

import (
	"bufio"
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"runtime"
	"strings"
	"time"

	"github.com/gorilla/websocket"
)

var input = bufio.NewReader(os.Stdin)

func main() {
	fmt.Println("Pocket Option — получение SSID для вашего бота")
	fmt.Println("Входите вручную в отдельном окне Edge/Chrome. Пароль вводится только на сайте.")
	if err := run(); err != nil {
		fmt.Println("Ошибка:", err)
	}
	fmt.Println("Нажмите Enter, чтобы закрыть окно.")
	input.ReadString('\n')
}

func browserPath() (string, error) {
	for _, base := range []string{os.Getenv("PROGRAMFILES(X86)"), os.Getenv("PROGRAMFILES"), os.Getenv("LOCALAPPDATA")} {
		if base == "" {
			continue
		}
		for _, rel := range []string{`Microsoft\Edge\Application\msedge.exe`, `Google\Chrome\Application\chrome.exe`} {
			p := filepath.Join(base, rel)
			if s, e := os.Stat(p); e == nil && !s.IsDir() {
				return p, nil
			}
		}
	}
	return "", errors.New("установите Microsoft Edge или Google Chrome")
}

func trustedPage(raw string) bool {
	u, e := url.Parse(raw)
	if e != nil || u.Scheme != "https" {
		return false
	}
	h := strings.ToLower(u.Hostname())
	return h == "pocketoption.com" || strings.HasSuffix(h, ".pocketoption.com") || h == "po.trade" || strings.HasSuffix(h, ".po.trade")
}

func validAuth(raw string) bool {
	if !strings.HasPrefix(raw, "42[") || strings.ContainsAny(raw, "\r\n\x00") {
		return false
	}
	var frame []json.RawMessage
	if json.Unmarshal([]byte(raw[2:]), &frame) != nil || len(frame) < 2 {
		return false
	}
	var event string
	var auth map[string]json.RawMessage
	if json.Unmarshal(frame[0], &event) != nil || event != "auth" || json.Unmarshal(frame[1], &auth) != nil {
		return false
	}
	var session string
	if json.Unmarshal(auth["session"], &session) != nil || strings.TrimSpace(session) == "" {
		return false
	}
	_, uid := auth["uid"]
	return uid
}

var envKey = regexp.MustCompile(`^\s*(?:export\s+)?POCKET_OPTION_SSID\s*=`)

func updateEnv(old []byte, auth string) []byte {
	newline := "\n"
	if bytes.Contains(old, []byte("\r\n")) {
		newline = "\r\n"
	}
	text := strings.TrimPrefix(string(old), "\ufeff")
	lines := strings.Split(strings.ReplaceAll(text, "\r\n", "\n"), "\n")
	// python-dotenv single-quoted strings: escape backslashes before quotes.
	quoted := strings.ReplaceAll(strings.ReplaceAll(auth, `\`, `\\`), `'`, `\'`)
	replacement := "POCKET_OPTION_SSID='" + quoted + "'"
	out := make([]string, 0, len(lines)+1)
	found := false
	for _, line := range lines {
		if envKey.MatchString(line) {
			if !found {
				out = append(out, replacement)
				found = true
			}
		} else {
			out = append(out, line)
		}
	}
	if !found {
		if len(out) > 0 && out[len(out)-1] == "" {
			out = out[:len(out)-1]
		}
		out = append(out, replacement, "")
	}
	return []byte(strings.Join(out, newline))
}

func saveEnv(path, auth string) error {
	old, e := os.ReadFile(path)
	if e != nil {
		return errors.New("не удалось прочитать .env")
	}
	backup, e := os.CreateTemp(filepath.Dir(path), ".env.ssid-backup-*")
	if e != nil {
		return errors.New("не удалось создать резервную копию .env")
	}
	backupName := backup.Name()
	if _, e = backup.Write(old); e != nil {
		backup.Close()
		os.Remove(backupName)
		return errors.New("ошибка резервного копирования")
	}
	if e = backup.Close(); e != nil {
		return errors.New("ошибка сохранения резервной копии")
	}
	f, e := os.CreateTemp(filepath.Dir(path), ".env.ssid-tmp-*")
	if e != nil {
		return errors.New("не удалось подготовить обновление .env")
	}
	defer os.Remove(f.Name())
	if _, e = f.Write(updateEnv(old, auth)); e != nil {
		f.Close()
		return errors.New("ошибка записи .env")
	}
	if e = f.Close(); e != nil {
		return errors.New("ошибка закрытия .env")
	}
	current, e := os.ReadFile(path)
	if e != nil || !bytes.Equal(old, current) {
		return errors.New(".env изменён другой программой; повторите получение SSID")
	}
	if e = os.Rename(f.Name(), path); e != nil {
		return errors.New("не удалось заменить .env; закройте редактор и повторите")
	}
	fmt.Println("Сохранено только POCKET_OPTION_SSID. Резервная копия:", filepath.Base(backupName))
	return nil
}

func run() error {
	if runtime.GOOS != "windows" {
		return errors.New("эта программа предназначена для Windows")
	}
	exe, e := os.Executable()
	if e != nil {
		return errors.New("не удалось определить папку программы")
	}
	dir := filepath.Dir(exe)
	if _, e = os.Stat(filepath.Join(dir, "main.py")); e != nil {
		return errors.New("поместите PocketOptionSSID.exe в папку бота рядом с main.py и .env")
	}
	env := filepath.Join(dir, ".env")
	if _, e = os.Stat(env); e != nil {
		return errors.New("рядом с программой отсутствует рабочий .env")
	}
	browser, e := browserPath()
	if e != nil {
		return e
	}
	profile, e := os.MkdirTemp("", "pocket-ssid-profile-")
	if e != nil {
		return errors.New("не удалось создать временный профиль")
	}
	defer os.RemoveAll(profile)
	cmd := exec.Command(browser, "--user-data-dir="+profile, "--remote-debugging-address=127.0.0.1", "--remote-debugging-port=0", "--no-first-run", "--no-default-browser-check", "about:blank")
	if e = cmd.Start(); e != nil {
		return errors.New("не удалось открыть браузер")
	}
	defer func() { exec.Command("taskkill", "/PID", fmt.Sprint(cmd.Process.Pid), "/T", "/F").Run(); cmd.Wait() }()
	var ws string
	deadline := time.Now().Add(30 * time.Second)
	for time.Now().Before(deadline) {
		data, err := os.ReadFile(filepath.Join(profile, "DevToolsActivePort"))
		if err == nil {
			parts := strings.Split(strings.TrimSpace(string(data)), "\n")
			if len(parts) >= 2 {
				ws = "ws://127.0.0.1:" + strings.TrimSpace(parts[0]) + strings.TrimSpace(parts[1])
				break
			}
		}
		time.Sleep(200 * time.Millisecond)
	}
	if ws == "" {
		return errors.New("браузер не предоставил локальное подключение; проверьте политики Edge/Chrome")
	}
	dialer := websocket.Dialer{HandshakeTimeout: 10 * time.Second}
	c, _, e := dialer.Dial(ws, http.Header{})
	if e != nil {
		return errors.New("не удалось подключиться к отдельному окну браузера")
	}
	defer func() {
		c.SetWriteDeadline(time.Now().Add(time.Second))
		c.WriteJSON(map[string]any{"id": 999999, "method": "Browser.close"})
		c.Close()
	}()
	c.SetReadLimit(4 << 20)
	c.SetReadDeadline(time.Now().Add(15 * time.Minute))
	id := 0
	send := func(session, method string, params any) error {
		id++
		msg := map[string]any{"id": id, "method": method, "params": params}
		if session != "" {
			msg["sessionId"] = session
		}
		c.SetWriteDeadline(time.Now().Add(10 * time.Second))
		return c.WriteJSON(msg)
	}
	if send("", "Target.setDiscoverTargets", map[string]any{"discover": true}) != nil {
		return errors.New("ошибка связи с браузером")
	}
	if send("", "Target.setAutoAttach", map[string]any{"autoAttach": true, "waitForDebuggerOnStart": false, "flatten": true}) != nil {
		return errors.New("ошибка подключения к вкладке")
	}
	type target struct {
		TargetID string `json:"targetId"`
		Type     string `json:"type"`
		URL      string `json:"url"`
	}
	sessions := map[string]string{}
	urls := map[string]string{}
	navigated := false
	fmt.Println("Открываю Pocket Option. Войдите в свой аккаунт и откройте торговую платформу.")
	fmt.Println("SSID не показывается и никуда не отправляется. Ожидание — до 15 минут.")
	for {
		var m struct {
			Method  string          `json:"method"`
			Session string          `json:"sessionId"`
			Params  json.RawMessage `json:"params"`
			Error   json.RawMessage `json:"error"`
		}
		if c.ReadJSON(&m) != nil {
			return errors.New("окно закрыто или время ожидания истекло; .env не изменён")
		}
		if len(m.Error) > 0 {
			return errors.New("браузер отклонил команду наблюдения; попробуйте актуальную версию Edge/Chrome")
		}
		switch m.Method {
		case "Target.targetInfoChanged":
			var p struct {
				Info target `json:"targetInfo"`
			}
			json.Unmarshal(m.Params, &p)
			urls[p.Info.TargetID] = p.Info.URL
		case "Target.attachedToTarget":
			var p struct {
				Session string `json:"sessionId"`
				Info    target `json:"targetInfo"`
			}
			json.Unmarshal(m.Params, &p)
			if p.Info.Type != "page" {
				continue
			}
			sessions[p.Session] = p.Info.TargetID
			urls[p.Info.TargetID] = p.Info.URL
			if send(p.Session, "Network.enable", map[string]any{}) != nil {
				return errors.New("не удалось включить наблюдение за вкладкой")
			}
			if !navigated {
				navigated = true
				if send(p.Session, "Page.navigate", map[string]any{"url": "https://pocketoption.com/"}) != nil {
					return errors.New("не удалось открыть сайт")
				}
			}
		case "Network.webSocketFrameSent":
			if !trustedPage(urls[sessions[m.Session]]) {
				continue
			}
			var p struct {
				Response struct {
					Opcode  int    `json:"opcode"`
					Payload string `json:"payloadData"`
				} `json:"response"`
			}
			json.Unmarshal(m.Params, &p)
			if p.Response.Opcode != 1 || !validAuth(p.Response.Payload) {
				continue
			}
			fmt.Println("Сообщение авторизации найдено. Сохранить SSID в .env? Введите Y и Enter:")
			answer, _ := input.ReadString('\n')
			if !strings.EqualFold(strings.TrimSpace(answer), "y") {
				fmt.Println("Отменено. .env не изменён.")
				return nil
			}
			if e = saveEnv(env, p.Response.Payload); e != nil {
				return e
			}
			fmt.Println("Теперь выполните в папке бота: docker compose up -d --force-recreate bot")
			fmt.Println("Получение SSID не проверяет поступление котировок; результат смотрите в логах бота.")
			return nil
		}
	}
}
