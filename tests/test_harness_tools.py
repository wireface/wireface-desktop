"""The Tools tab: reading each agent's MCP servers and skills, switching them its own way, keeping secrets out, and
installing from the catalogue. Every file here is a temporary copy: the real ~/.claude.json, ~/.codex and OpenCode's
config are never touched."""
import asyncio
import json
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

from wireface import harness_tools as ht
from wireface.activity import ActivityLog
from wireface.harness_tools import (ClaudeCode, Codex, ConfigError, HarnessTools, OpenCode, Writer, describe_server,
                                    install_plan, is_installed, parse_claude_list, parse_codex_list, parse_opencode_list,
                                    redact_url, scrub)
from wireface.transcript import find_tool_result

KEY = "sk-live-0123456789abcdefABCDEF"          # a made-up key: it must never come out the other end
PAT = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"


def dump(obj) -> str:
    return json.dumps(obj, default=str)


# --- secrets ------------------------------------------------------------------------------------------------------------
def test_a_servers_secrets_never_show():
    d = describe_server({"command": "npx", "args": ["-y", "some-mcp@1.2.3", f"--api-key={KEY}", "--token", PAT, f"KEY={KEY}",
                                                   f"https://api.example.com/mcp?key={KEY}&mode=x", "--port", "8080"],
                         "env": {"OPENAI_API_KEY": KEY, "DEBUG": "1"}})
    assert d["transport"] == "stdio" and d["env"] == ["DEBUG", "OPENAI_API_KEY"]
    assert KEY not in dump(d) and PAT not in dump(d)
    assert "some-mcp@1.2.3" in d["target"] and "--port 8080" in d["target"] and "mode=****" in d["target"]
    h = describe_server({"type": "http", "url": f"https://user:{KEY}@mcp.example.com/{KEY}/sse?token={KEY}",
                         "headers": {"Authorization": f"Bearer {PAT}"}})
    assert h == {"transport": "http", "target": "https://****@mcp.example.com/****/sse?token=****", "env": ["Authorization"],
                 "program": ""}
    w = describe_server({"type": "stdio", "command": "cmd", "args": ["/c", "npx", "-y", "@playwright/mcp@0.0.83"]})
    assert w["target"] == "npx -y @playwright/mcp@0.0.83"                  # Windows' wrapper left out
    oc = describe_server({"type": "local", "command": ["uvx", "mcp-server-fetch"], "environment": {"X_TOKEN": KEY}})
    assert oc["transport"] == "stdio" and oc["target"] == "uvx mcp-server-fetch" and oc["env"] == ["X_TOKEN"]


def test_free_text_is_scrubbed():
    text = f"failed: 401 for https://x.dev/mcp?api_key={KEY} with Authorization: Bearer {PAT}; token={KEY}; own secret-value-1"
    out = scrub(text, {"secret-value-1"})
    assert KEY not in out and PAT not in out and "secret-value-1" not in out and "failed: 401" in out
    assert redact_url("https://mcp.context7.com/mcp") == "https://mcp.context7.com/mcp"


# --- Claude Code --------------------------------------------------------------------------------------------------------
@pytest.fixture
def claude(tmp_path):
    """A home with a .claude.json (a user server, a local one, a project entry), a project with .mcp.json in a git
    repository, skills of every kind and a skill switched off in the user's settings."""
    home, repo = tmp_path / "home", tmp_path / "work" / "repo"
    sub = repo / "app"
    (repo / ".git").mkdir(parents=True)
    sub.mkdir()
    key = repo.as_posix()
    (home / ".claude" / "skills" / "notes").mkdir(parents=True)
    (home / ".claude" / "skills" / "notes" / "SKILL.md").write_text("---\nname: notes\ndescription: Keeps notes.\n---\nx\n")
    (home / ".claude" / "skills" / "old").mkdir()
    (home / ".claude" / "skills" / "old" / "SKILL.md").write_text("---\nname: old\ndescription: >\n  Folded\n  text.\n---\n")
    synced = home / ".claude" / "skills" / "synced" / "bucket" / "pdf"
    synced.mkdir(parents=True)
    (synced / "SKILL.md").write_text("---\nname: pdf\ndescription: PDFs.\n---\n")
    (repo / ".claude" / "skills" / "deploy").mkdir(parents=True)
    (repo / ".claude" / "skills" / "deploy" / "SKILL.md").write_text("---\nname: deploy\ndescription: Ships it.\n---\n")
    (home / ".claude" / "settings.json").write_text(json.dumps({"skillOverrides": {"old": "off"}, "model": "opus"}))
    config = {"numStartups": 3, "mcpServers": {"ctx": {"type": "http", "url": "https://mcp.context7.com/mcp"},
                                               "secretive": {"command": "npx", "args": ["-y", "x-mcp"], "env": {"API_KEY": KEY}}},
              "projects": {key: {"mcpServers": {"mine": {"command": "python", "args": ["srv.py"]}},
                                 "disabledMcpServers": ["ctx"], "lastCost": 1.5}}}
    (home / ".claude.json").write_text(json.dumps(config, indent=2))
    (repo / ".mcp.json").write_text(json.dumps({"mcpServers": {"team": {"type": "http", "url": "https://team.example/mcp"}}}))
    return SimpleNamespace(home=home, repo=repo, sub=sub, key=key, writer=Writer(tmp_path / "backups"))


def test_claude_codes_servers_and_skills(claude):
    c = ClaudeCode(str(claude.sub), home=claude.home, env={})
    assert c.project == claude.repo and c.project_key == claude.key                # the git root, with forward slashes
    servers = {s.name: s for s in c.servers()}
    assert {n: (s.scope, s.enabled) for n, s in servers.items()} == {
        "mine": ("local", True), "team": ("project", True), "ctx": ("user", False), "secretive": ("user", True)}
    assert servers["secretive"].env == ["API_KEY"] and KEY not in dump([s.to_json() for s in servers.values()])
    assert KEY in c.secrets
    skills = {s.name: s for s in c.skills()}
    assert {n: (s.scope, s.enabled) for n, s in skills.items()} == {
        "notes": ("user", True), "old": ("user", False), "deploy": ("project", True), "pdf": ("claude.ai", True)}
    assert skills["old"].description == "Folded text." and not skills["pdf"].can_toggle
    assert not c.errors


def test_a_project_server_turned_off_or_rejected(claude):
    data = json.loads((claude.home / ".claude.json").read_text())
    data["projects"][claude.key]["disabledMcpServers"] = ["team"]
    (claude.home / ".claude.json").write_text(json.dumps(data, indent=2))
    team = next(s for s in ClaudeCode(str(claude.sub), home=claude.home, env={}).servers() if s.name == "team")
    assert team.scope == "project" and not team.enabled and team.can_toggle
    data["projects"][claude.key]["disabledMcpjsonServers"] = ["team"]
    (claude.home / ".claude.json").write_text(json.dumps(data, indent=2))
    team = next(s for s in ClaudeCode(str(claude.sub), home=claude.home, env={}).servers() if s.name == "team")
    assert not team.enabled and not team.can_toggle and "disabledMcpjsonServers" in team.why


def test_claude_switches_a_server_as_mcp_does(claude):
    c = ClaudeCode(str(claude.sub), home=claude.home, env={})
    before = (claude.home / ".claude.json").read_text()
    s = next(s for s in c.servers() if s.name == "secretive")
    backup = c.set_server(claude.writer, s, False)
    data = json.loads((claude.home / ".claude.json").read_text())
    assert data["projects"][claude.key]["disabledMcpServers"] == ["ctx", "secretive"]
    assert data["projects"][claude.key]["lastCost"] == 1.5 and data["numStartups"] == 3          # the rest kept
    assert backup and backup.read_text() == before                                                 # a copy first
    assert (claude.home / ".claude.json").read_text() == json.dumps(data, indent=2)               # its formatting
    c.set_server(claude.writer, next(s for s in ClaudeCode(str(claude.sub), home=claude.home, env={}).servers()
                                     if s.name == "ctx"), True)
    assert json.loads((claude.home / ".claude.json").read_text())["projects"][claude.key]["disabledMcpServers"] == ["secretive"]
    assert claude.writer.backup_path(claude.home / ".claude.json").read_text() == before         # the first copy kept


def test_claude_config_dir_moves_everything(claude, tmp_path):
    moved = tmp_path / "elsewhere"
    moved.mkdir()
    (moved / ".claude.json").write_text(json.dumps({"mcpServers": {"only-here": {"command": "x"}}}))
    c = ClaudeCode(str(claude.sub), home=claude.home, env={"CLAUDE_CONFIG_DIR": str(moved)})
    assert c.global_file == moved / ".claude.json" and [s.name for s in c.servers()] == ["team", "only-here"]
    assert c.skills_home() == moved / "skills"


def test_claude_switches_a_skill_as_skills_does(claude):
    c = ClaudeCode(str(claude.sub), home=claude.home, env={})
    skills = {s.name: s for s in c.skills()}
    c.set_skill(claude.writer, skills["notes"], False)
    local = claude.repo / ".claude" / "settings.local.json"
    assert json.loads(local.read_text()) == {"skillOverrides": {"notes": "off"}}
    c.set_skill(claude.writer, skills["notes"], True)
    assert json.loads(local.read_text()) == {}
    c.set_skill(claude.writer, skills["old"], True)                     # off in the user's settings: "on" here wins
    assert json.loads(local.read_text()) == {"skillOverrides": {"old": "on"}}
    assert {s.name: s.enabled for s in ClaudeCode(str(claude.sub), home=claude.home, env={}).skills()}["old"]


def test_parse_claude_mcp_list():
    text = ("Checking MCP server health\u2026\n\nctx: https://mcp.context7.com/mcp (HTTP) - \u2714 Connected\n"
            "claude.ai Gmail: https://gmail.mcp.claude.com/mcp - ! Needs authentication\n"
            f"bad: npx -y x --api-key={KEY} - \u2718 Failed to connect\n"
            "team: https://team.example/mcp (HTTP) - \u23f8 Pending approval (run `claude` to approve)\n"
            "off: npx y - \u2298 Disabled for this project (re-enable via /mcp)\n")
    got = parse_claude_list(text, ["ctx", "bad", "team", "off"])
    assert {n: s for n, (s, _) in got.items()} == {"ctx": "connected", "claude.ai Gmail": "needs-auth", "bad": "failed",
                                                   "team": "unknown", "off": "disabled"}
    assert KEY not in dump(got)


# --- Codex ----------------------------------------------------------------------------------------------------------------
CODEX_TOML = """# my Codex settings
model = "gpt-5"

[mcp_servers.playwright]
command = "playwright-mcp"   # the browser

[mcp_servers."my server"]
command = "npx"
args = ["-y", "x"]

[mcp_servers."my server".env]
API_KEY = "%s"

[projects.'C:\\work']
trust_level = "trusted"
""" % KEY


@pytest.fixture
def codex(tmp_path):
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex" / "config.toml").write_bytes(CODEX_TOML.replace("\n", "\r\n").encode())
    (home / ".agents" / "skills" / "notes").mkdir(parents=True)
    (home / ".agents" / "skills" / "notes" / "SKILL.md").write_text("---\nname: notes\ndescription: Notes.\n---\n")
    (home / ".codex" / "skills" / ".system" / "imagegen").mkdir(parents=True)
    (home / ".codex" / "skills" / ".system" / "imagegen" / "SKILL.md").write_text("---\nname: imagegen\ndescription: I.\n---\n")
    work = tmp_path / "work"
    work.mkdir()
    return SimpleNamespace(home=home, work=work, file=home / ".codex" / "config.toml", writer=Writer(tmp_path / "backups"))


def test_codex_servers_switch_with_enabled(codex):
    c = Codex(str(codex.work), home=codex.home, env={})
    servers = {s.name: s for s in c.servers()}
    assert set(servers) == {"playwright", "my server"} and all(s.enabled for s in servers.values())
    assert servers["my server"].env == ["API_KEY"] and KEY in c.secrets and KEY not in dump(servers["my server"].to_json())
    c.set_server(codex.writer, servers["my server"], False)
    text = codex.file.read_bytes().decode()
    assert "\r\n" in text and text.count("\n") == text.count("\r\n")                        # its line endings
    before, after = CODEX_TOML.splitlines(), text.split("\r\n")
    assert [line for line in after if line not in before] == ["enabled = false"]               # one line more, nothing else
    assert after.index("enabled = false") == after.index('[mcp_servers."my server"]') + 1
    assert tomllib.loads(text)["mcp_servers"]["my server"]["enabled"] is False
    assert not [s for s in Codex(str(codex.work), home=codex.home, env={}).servers() if s.name == "my server"][0].enabled
    c.set_server(codex.writer, servers["my server"], True)
    assert tomllib.loads(codex.file.read_text())["mcp_servers"]["my server"]["enabled"] is True
    assert "# the browser" in codex.file.read_text()                                           # comments kept


def test_codex_inline_tables_are_left_alone(tmp_path):
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex" / "config.toml").write_text('[mcp_servers]\nx = { command = "y" }\n')
    c = Codex(str(tmp_path), home=home, env={})
    with pytest.raises(ConfigError, match="by hand"):
        c.set_server(Writer(tmp_path / "b"), c.servers()[0], False)
    assert (home / ".codex" / "config.toml").read_text() == '[mcp_servers]\nx = { command = "y" }\n'


def test_codex_skills_switch_with_skills_config(codex):
    c = Codex(str(codex.work), home=codex.home, env={"CODEX_HOME": str(codex.home / ".codex")})
    skills = {s.name: s for s in c.skills()}
    assert {n: s.scope for n, s in skills.items()} == {"notes": "user", "imagegen": "system"}
    c.set_skill(codex.writer, skills["notes"], False)
    data = tomllib.loads(codex.file.read_text())
    assert data["skills"]["config"] == [{"path": str(Path(skills["notes"].path) / "SKILL.md"), "enabled": False}]
    assert data["mcp_servers"]["my server"]["env"]["API_KEY"] == KEY                         # the rest as it was
    assert not {s.name: s.enabled for s in Codex(str(codex.work), home=codex.home, env={}).skills()}["notes"]
    c.set_skill(codex.writer, skills["notes"], True)
    assert codex.file.read_bytes().decode().rstrip("\r\n") == CODEX_TOML.replace("\n", "\r\n").rstrip("\r\n")


def test_parse_codex_list_keeps_its_secrets():
    text = json.dumps([
        {"name": "a", "enabled": True, "auth_status": "unsupported",
         "transport": {"type": "stdio", "command": "npx", "args": ["x"], "env": {"TOKEN": KEY}}},
        {"name": "b", "enabled": False, "disabled_reason": "", "transport": {"type": "streamable_http", "url": "https://b.dev/mcp"}},
        {"name": "c", "enabled": True, "auth_status": "not_logged_in", "transport": {"type": "streamable_http", "url": "https://c.dev/mcp",
                                                                                    "http_headers": {"X-Key": KEY}}}])
    got = parse_codex_list("WARNING: something\n" + text)
    assert {n: r["status"] for n, r in got.items()} == {"a": "unknown", "b": "disabled", "c": "needs-auth"}
    assert got["c"]["describe"]["transport"] == "http" and KEY not in dump(got)


# --- OpenCode ------------------------------------------------------------------------------------------------------------
@pytest.fixture
def opencode(tmp_path):
    home, work = tmp_path / "home", tmp_path / "work"
    (home / ".config" / "opencode").mkdir(parents=True)
    work.mkdir()
    (home / ".config" / "opencode" / "opencode.json").write_text(json.dumps({
        "$schema": "https://opencode.ai/config.json",
        "mcp": {"v1": {"type": "local", "command": ["npx", "-y", "x"], "environment": {"K": KEY}, "enabled": True},
                "servers": {"v2": {"type": "remote", "url": "https://v2.dev/mcp", "headers": {"Authorization": f"Bearer {KEY}"}}}},
        "permission": {"skill": {"old": "deny"}}}, indent=2))
    (work / "opencode.jsonc").write_text('{\n  // the team\'s\n  "mcp": {"team": {"type": "remote", "url": "https://t.dev"}}\n}\n')
    for name in ("old", "new"):
        (home / ".claude" / "skills" / name).mkdir(parents=True)
        (home / ".claude" / "skills" / name / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {name}.\n---\n")
    return SimpleNamespace(home=home, work=work, file=home / ".config" / "opencode" / "opencode.json",
                           writer=Writer(tmp_path / "backups"))


def test_opencode_servers_both_shapes(opencode):
    o = OpenCode(str(opencode.work), home=opencode.home, env={})
    servers = {s.name: s for s in o.servers()}
    assert {n: (s.scope, s.enabled, s.transport) for n, s in servers.items()} == {
        "v1": ("global", True, "stdio"), "v2": ("global", True, "http"), "team": ("project", True, "http")}
    assert KEY not in dump([s.to_json() for s in servers.values()])
    o.set_server(opencode.writer, servers["v1"], False)
    o.set_server(opencode.writer, servers["v2"], False)
    data = json.loads(opencode.file.read_text())
    assert data["mcp"]["v1"]["enabled"] is False and data["mcp"]["servers"]["v2"]["disabled"] is True
    assert data["$schema"] and data["mcp"]["v1"]["environment"]["K"] == KEY
    with pytest.raises(ConfigError, match="comments"):                    # a .jsonc with comments: not rewritten
        o.set_server(opencode.writer, servers["team"], False)
    assert "// the team's" in (opencode.work / "opencode.jsonc").read_text()


def test_opencode_skills_switch_with_permission_rules(opencode):
    o = OpenCode(str(opencode.work), home=opencode.home, env={})
    skills = {s.name: s for s in o.skills()}
    assert {n: s.enabled for n, s in skills.items()} == {"old": False, "new": True}
    o.set_skill(opencode.writer, skills["new"], False)                    # v1 (or unknown): permission.skill
    assert json.loads(opencode.file.read_text())["permission"]["skill"] == {"old": "deny", "new": "deny"}
    o.set_skill(opencode.writer, skills["old"], True)
    assert json.loads(opencode.file.read_text())["permission"]["skill"] == {"new": "deny"}
    v2 = OpenCode(str(opencode.work), home=opencode.home, env={}, version=(2, 0, 22))
    v2.set_skill(opencode.writer, {s.name: s for s in v2.skills()}["old"], False)
    assert json.loads(opencode.file.read_text())["permissions"] == [{"action": "skill", "resource": "old", "effect": "deny"}]
    v2.set_skill(opencode.writer, {s.name: s for s in v2.skills()}["old"], True)
    assert "permissions" not in json.loads(opencode.file.read_text())


def test_parse_opencode_list():
    text = ("\x1b[32m\u2713\x1b[0m playwright  connected\n\u26a0 linear  needs authentication\n"
            f"\u2717 broken  failed: spawn npx ENOENT {KEY}\n\u25cb later  disabled\n")
    got = parse_opencode_list(text, ["playwright", "linear", "broken", "later"])
    assert {n: s for n, (s, _) in got.items()} == {"playwright": "connected", "linear": "needs-auth", "broken": "failed",
                                                   "later": "disabled"}
    assert got["broken"][1].startswith("spawn npx ENOENT") and KEY not in dump(got)


# --- the catalogue ------------------------------------------------------------------------------------------------------
def test_the_catalogue_and_how_each_item_installs(tmp_path):
    cat = ht.catalogue()
    ids = [i["id"] for i in cat["mcp"]]
    assert {"context7", "playwright", "github", "memory"} <= set(ids)
    own = [i for i in cat["skills"] if i.get("bundled")]
    assert len(own) >= 4 and all(Path(i["bundled"], "SKILL.md").is_file() for i in own)
    for i in cat["mcp"]:                                                     # every package is pinned
        assert i.get("remote") or "@" in (i.get("npx") or i.get("uvx") or "").lstrip("@")
    item = {i["id"]: i for i in cat["mcp"] + cat["skills"]}
    home = tmp_path / "h"
    c = ClaudeCode(str(tmp_path), home=home, env={})
    assert install_plan(item["context7"], c)["command"] == \
        "claude mcp add --scope user --transport http context7 https://mcp.context7.com/mcp"
    mem = install_plan(item["memory"], c, windows=True)
    assert mem["by"] == "agent" and f"MEMORY_FILE_PATH={home.as_posix()}/.mcp-memory/memory.jsonl" in mem["command"]
    assert "cmd /c" not in mem["command"] and "exactly this command" in mem["prompt"]
    assert install_plan(item["github"], c)["command"] == "claude plugin install github@claude-plugins-official --scope user"
    x = Codex(str(tmp_path), home=home, env={})
    pw = install_plan(item["playwright"], x, windows=True)
    assert pw["by"] == "wireface" and pw["argv"] == ["codex", "mcp", "add", "playwright", "--", "cmd", "/c", "npx", "-y",
                                                     "@playwright/mcp@0.0.83"]
    gh = install_plan(item["github"], x)
    assert gh["argv"][-2:] == ["--bearer-token-env-var", "GITHUB_PERSONAL_ACCESS_TOKEN"]
    assert install_plan(item["webapp-testing"], x)["by"] == "you"
    assert install_plan(item["webapp-testing"], x, skip_permissions=True)["by"] == "agent"
    o = OpenCode(str(tmp_path), home=home, env={})
    oc = install_plan(item["github"], o)
    assert oc["by"] == "agent" and '"Authorization": "Bearer {env:GITHUB_PERSONAL_ACCESS_TOKEN}"' in oc["prompt"]
    assert install_plan(own[0], o) == {"by": "wireface", "command": f"Copy the folder {own[0]['id']} to {o.skills_home() / own[0]['id']}",
                                       "dest": str(o.skills_home() / own[0]["id"]), "note": "Wireface copies it."}
    # already there: by name, by package or by address
    s = lambda name, target: ht.Server(name=name, scope="user", target=target)
    assert is_installed(item["playwright"], [s("pw", "npx -y @playwright/mcp@0.0.70")], [])
    assert is_installed(item["context7"], [s("docs", "https://mcp.context7.com/mcp")], [])
    assert is_installed(item["github"], [s("plugin:github:github", "")], [])
    assert not is_installed(item["memory"], [s("pw", "npx -y @playwright/mcp")], [])


# --- the service, with a stand-in for the app --------------------------------------------------------------------------
class FakeBackend:
    def __init__(self, kind="claude", connected=False):
        self.id, self.label, self.connected = kind, {"claude": "Claude Code", "codex": "Codex", "opencode": "OpenCode"}[kind], connected
        self.sent, self.toggles, self.reconnected = [], [], 0
        self.client = object()                           # the session (a new one each time it starts)

    def send(self, text):
        self.sent.append(text)

    def reconnect(self):
        self.reconnected += 1

    async def mcp_status(self):
        return [{"name": "secretive", "status": "failed", "error": f"spawn failed with {KEY}", "scope": "user",
                 "config": {"command": "npx", "env": {"API_KEY": KEY}}},
                {"name": "claude.ai Gmail", "status": "needs-auth", "scope": "claudeai",
                 "config": {"type": "claudeai-proxy", "url": "https://gmail.mcp.claude.com/mcp"}},
                {"name": "face", "status": "connected", "scope": "dynamic", "tools": [{"name": "express"}, {"name": "show_face"}]}]

    async def toggle_mcp(self, name, enabled):
        self.toggles.append((name, enabled))


def fake_host(tmp_path, cwd, kind="claude", connected=False, **cfg):
    host = SimpleNamespace(backend=FakeBackend(kind, connected), backend_cfg={"cwd": str(cwd), "tools": {}, **cfg},
                           activity=ActivityLog(), clients=set(), broadcast=lambda m: None, cfg={"phone": {"enabled": False}},
                           credentials=SimpleNamespace(secret_values=lambda: {"my-own-gemini-key-123"}))

    async def set_config(patch):
        host.patched = patch
    host.set_config = set_config
    return host


def test_the_view_merges_the_live_status_and_keeps_secrets_out(claude, tmp_path):
    host = fake_host(tmp_path, claude.sub, connected=True)
    tools = HarnessTools(host, backups=tmp_path / "b", home=claude.home, env={})

    async def go():
        await tools._check(0)
        return tools.view()
    view = asyncio.run(go())
    servers = {s["name"]: s for s in view["servers"]}
    assert servers["secretive"]["status"] == "failed" and "[hidden]" in servers["secretive"]["detail"]
    assert servers["claude.ai Gmail"]["scope"] == "claude.ai" and servers["claude.ai Gmail"]["status"] == "needs-auth"
    assert servers["face"]["scope"] == "wireface" and servers["face"]["tools"] == 2 and not servers["face"]["can_toggle"]
    assert servers["browser"]["setting"] == "browser" and servers["ctx"]["status"] == "disabled"
    assert view["check"]["source"] == "the running session" and KEY not in dump(view)
    rec = {r["id"]: r for r in view["recommended"]}
    assert rec["context7"]["installed"] and rec["playwright"]["builtin"] and not rec["memory"]["installed"]
    assert rec["memory"]["plan"]["by"] == "agent" and "prompt" not in rec["memory"]["plan"]
    # what the rows' details and the cards show
    assert servers["face"]["tool_names"] == ["express", "show_face"] and "program" not in servers["secretive"]
    assert servers["secretive"]["writes"] == str(claude.home / ".claude.json") and "disabledMcpServers" in servers["secretive"]["how"]
    assert servers["browser"]["how"] == "backend.tools.browser" and servers["face"]["description"]
    assert rec["context7"]["installed_key"] == "user:ctx" and rec["playwright"]["installed_key"] == "wireface:browser"
    assert [n["label"] for n in rec["github"]["needs"]] == ["GitHub token"] and isinstance(rec["github"]["needs"][0]["ok"], bool)
    assert "GITHUB_PERSONAL_ACCESS_TOKEN" in rec["github"]["needs"][0]["hint"]
    own = rec["dictation-notes"]
    assert own["by"] == "Made for Wireface" and len(own["description"]) < 160 and own["needs"] == []
    skills = {s["name"]: s for s in view["skills"]}
    assert skills["notes"]["writes"].endswith("settings.local.json") and skills["notes"]["how"] == "skillOverrides"
    assert view["home"] == str(claude.home) and view["running"]


def test_a_server_whose_program_is_missing_is_a_problem(claude, tmp_path):
    data = json.loads((claude.home / ".claude.json").read_text())
    data["mcpServers"]["gone"] = {"command": "no-such-program-for-wireface", "args": ["--serve"]}
    data["mcpServers"]["here"] = {"command": "cmd", "args": ["/c", sys.executable, "-V"]}
    (claude.home / ".claude.json").write_text(json.dumps(data, indent=2))
    tools = HarnessTools(fake_host(tmp_path, claude.sub), backups=tmp_path / "b", home=claude.home, env={})
    servers = {s["name"]: s for s in tools.view()["servers"]}
    assert "Can't find no-such-program-for-wireface" in servers["gone"]["missing"]
    assert servers["here"]["missing"] == ""                    # cmd /c <python>: python is what has to be there


def test_switching_uses_the_live_session_or_the_file(claude, tmp_path):
    host = fake_host(tmp_path, claude.sub, connected=True)
    tools = HarnessTools(host, backups=tmp_path / "b", home=claude.home, env={})

    async def live():
        said = await tools.set_server("user:secretive", False)
        await asyncio.sleep(0)
        return said
    assert "Applied now" in asyncio.run(live()) and host.backend.toggles == [("secretive", False)]
    assert "disabledMcpServers" in (claude.home / ".claude.json").read_text() and \
        json.loads((claude.home / ".claude.json").read_text())["projects"][claude.key]["disabledMcpServers"] == ["ctx"]
    host.backend.connected = False                                       # no session: Wireface writes what /mcp would
    assert "when it starts" in asyncio.run(tools.set_server("user:secretive", False))
    assert json.loads((claude.home / ".claude.json").read_text())["projects"][claude.key]["disabledMcpServers"] == ["ctx", "secretive"]
    asyncio.run(tools.set_server("wireface:browser", False))            # Wireface's own: its setting
    assert host.patched == {"backend": {"tools": {"browser": False}}}
    with pytest.raises(ValueError, match="Part of the face"):
        asyncio.run(tools.set_server("wireface:face", False))
    notes = [e["summary"] for e in host.activity.recent() if e["kind"] == "notice"]
    assert notes[:2] == ["Turned the MCP server secretive off"] * 2 and KEY not in dump(host.activity.recent())


def test_a_skill_switch_waits_for_a_restart(claude, tmp_path):
    host = fake_host(tmp_path, claude.sub, connected=True)
    tools = HarnessTools(host, backups=tmp_path / "b", home=claude.home, env={})
    tools._session = host.backend.client

    async def go():
        assert "picks it up when it restarts" in await tools.set_skill("user:notes", False)
        assert tools.pending.startswith("Restart Claude Code")
        tools.on_activity(SimpleNamespace(kind="notice", status="ok"))   # the same session: still waiting
        assert tools.pending and tools._task is None
        await tools.restart()
        assert host.backend.reconnected == 1
        host.backend.client = object()                                   # it started again: the change is in
        tools.on_activity(SimpleNamespace(kind="notice", status="ok"))
        assert tools.pending is None and tools._task is not None         # and its servers are checked again
        tools._task.cancel()
    asyncio.run(go())


def test_installing(claude, tmp_path):
    host = fake_host(tmp_path, claude.sub, connected=True)
    tools = HarnessTools(host, backups=tmp_path / "b", home=claude.home, env={})
    said = asyncio.run(tools.install("dictation-notes"))                 # Wireface's own skill: copied in
    dest = claude.home / ".claude" / "skills" / "dictation-notes" / "SKILL.md"
    assert dest.is_file() and "Installed" in said
    with pytest.raises(ValueError, match="already"):
        asyncio.run(tools.install("dictation-notes"))
    said = asyncio.run(tools.install("memory"))                          # an MCP server: the agent is asked
    assert host.backend.sent and "claude mcp add --scope user memory" in host.backend.sent[-1] and "Activity" in said
    assert {r["id"]: r for r in tools.view()["recommended"]}["memory"]["installing"]
    with pytest.raises(ValueError, match="isn't in the catalogue"):
        asyncio.run(tools.install("nope"))


def test_codex_installs_with_its_own_command(codex, tmp_path):
    host = fake_host(tmp_path, codex.work, kind="codex")
    host.backend._exe = lambda: "C:/bin/codex.exe"
    tools = HarnessTools(host, backups=tmp_path / "b", home=codex.home, env={})
    ran = []

    async def run(argv, timeout=0):
        ran.append(argv)
        return ""
    tools._run = run

    async def go():
        said = await tools.install("context7")
        tools._task.cancel()
        return said
    assert "next message" in asyncio.run(go())
    assert ran == [["C:/bin/codex.exe", "mcp", "add", "context7", "--url", "https://mcp.context7.com/mcp"]]
    assert host.backend.sent == []                                       # not the agent: its sandbox can't


# --- the API and the transcript's full outputs ---------------------------------------------------------------------------
def test_full_tool_output_from_the_transcripts(tmp_path):
    folder = tmp_path / "proj"
    folder.mkdir()
    big = "line\n" * 5000
    old, current = folder / "old.jsonl", folder / "cur.jsonl"
    old.write_text(json.dumps({"type": "user", "uuid": "u", "timestamp": "2026-10-07T10:00:00Z", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "toolu_1", "content": big}]}}) + "\n")
    current.write_text('{"type": "user", "message": {"content": "hi"}}\n')
    got = find_tool_result(folder, "toolu_1", first=current)
    assert got["text"] == big and got["session"] == "old" and not got["is_error"]
    assert find_tool_result(folder, "toolu_2", first=current) is None


def test_the_api(tmp_path, monkeypatch, claude):
    from aiohttp.test_utils import TestClient, TestServer

    from wireface import config, profile, server
    from wireface.app import App

    monkeypatch.setattr(config, "DATA", tmp_path / "data")
    monkeypatch.setattr(config, "DEFAULT_CWD", claude.sub)
    monkeypatch.setenv("USERPROFILE", str(claude.home))
    monkeypatch.setenv("HOME", str(claude.home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    app = App(profile.resolve("tools-test"), shell=False)
    app.tools.home = claude.home

    async def go():
        async with TestClient(TestServer(server.build(app))) as client:
            auth = {"X-Face-Token": app.token}
            r = await client.get("/api/tools", headers=auth)
            view = (await r.json())["view"]
            assert r.status == 200 and view["harness"]["type"] == "claude" and KEY not in await r.text()
            assert {s["name"] for s in view["servers"]} >= {"face", "secretive", "team"}
            r = await client.put("/api/tools/skill", json={"key": "user:notes", "enabled": False}, headers=auth)
            assert r.status == 200 and "notes turned off" in (await r.json())["message"]
            r = await client.put("/api/tools/server", json={"key": "user:nothing", "enabled": False}, headers=auth)
            assert r.status == 400 and "refresh" in (await r.json())["error"]
            r = await client.get("/api/transcript/result?id=toolu_x", headers=auth)
            assert (await r.json()) == {"found": False}
            assert (await client.get("/api/tools")).status == 401         # the token, as everywhere
    asyncio.run(go())
    assert json.loads((claude.repo / ".claude" / "settings.local.json").read_text()) == {"skillOverrides": {"notes": "off"}}
