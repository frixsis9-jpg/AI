package main

import (
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func authFrame(session string) string {
	b, _ := json.Marshal([]any{"auth", map[string]any{"session": session, "uid": 123, "isDemo": 1}})
	return "42" + string(b)
}

func TestAcceptOnlyAuthWithSessionAndUID(t *testing.T) {
	for _, s := range []string{"42[\"auth\",{}]", `42["auth",{"session":"x"}]`, `42["tick",{"session":"x","uid":1}]`, `42["auth",{"session":null,"uid":1}]`, "password", authFrame("x") + "\n"} {
		if validAuth(s) {
			t.Fatal("accepted invalid frame")
		}
	}
	if !validAuth(authFrame("local-test-session")) {
		t.Fatal("rejected auth")
	}
}

func TestScopeToPocketPages(t *testing.T) {
	for _, s := range []string{"https://pocketoption.com/", "https://pocketoption.com/en/cabinet/", "https://m.po.trade/"} {
		if !trustedPage(s) {
			t.Fatal("rejected trusted page")
		}
	}
	for _, s := range []string{"http://pocketoption.com/", "https://pocketoption.com.evil.example/", "https://evilpocketoption.com/", "https://pocketoption.com@evil.example/", "about:blank"} {
		if trustedPage(s) {
			t.Fatal("accepted foreign page")
		}
	}
}

func TestUpdatePreservesOtherSettingsAndEscapesValue(t *testing.T) {
	old := []byte("# settings\r\nTELEGRAM_BOT_TOKEN=untouched\r\nPOCKET_OPTION_SSID=old\r\nDATABASE_PATH=signals.db\r\nexport POCKET_OPTION_SSID=duplicate\r\n")
	updated := string(updateEnv(old, authFrame(`session'with\slash`)))
	if !strings.Contains(updated, "TELEGRAM_BOT_TOKEN=untouched\r\n") || !strings.Contains(updated, "DATABASE_PATH=signals.db\r\n") {
		t.Fatal("changed unrelated settings")
	}
	if strings.Count(updated, "POCKET_OPTION_SSID=") != 1 {
		t.Fatal("duplicate keys")
	}
	if !strings.Contains(updated, `\'`) || !strings.Contains(updated, `\\`) {
		t.Fatal("escaping missing")
	}
	if strings.Contains(strings.ReplaceAll(updated, "\r\n", ""), "\n") {
		t.Fatal("changed line endings")
	}
}

func TestSaveBackupAndRepeatedUpdates(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, ".env")
	old := []byte("# keep\nTOKEN=untouched\n")
	if e := os.WriteFile(path, old, 0600); e != nil {
		t.Fatal(e)
	}
	first := authFrame("first-session")
	if e := saveEnv(path, first); e != nil {
		t.Fatal(e)
	}
	got, _ := os.ReadFile(path)
	if !bytes.Equal(got, updateEnv(old, first)) {
		t.Fatal("wrong save")
	}
	backups, _ := filepath.Glob(filepath.Join(dir, ".env.ssid-backup-*"))
	if len(backups) != 1 {
		t.Fatal("missing backup")
	}
	backup, _ := os.ReadFile(backups[0])
	if !bytes.Equal(backup, old) {
		t.Fatal("backup differs")
	}
	if e := saveEnv(path, authFrame("second-session")); e != nil {
		t.Fatal(e)
	}
	got, _ = os.ReadFile(path)
	if bytes.Contains(got, []byte("first-session")) || !bytes.Contains(got, []byte("second-session")) {
		t.Fatal("repeat update failed")
	}
}

func TestMissingEnvNotCreated(t *testing.T) {
	p := filepath.Join(t.TempDir(), ".env")
	if saveEnv(p, authFrame("session")) == nil {
		t.Fatal("missing env accepted")
	}
	if _, e := os.Stat(p); !os.IsNotExist(e) {
		t.Fatal("created env")
	}
}
