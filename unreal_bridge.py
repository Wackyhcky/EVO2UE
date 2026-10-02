"""
unreal_bridge.py - talk to a running Unreal Editor from outside, or launch one.

Uses the Python Editor Script Plugin's *remote execution* protocol (the same one
Epic's remote_execution.py and Blender "Send to Unreal" use):
  * UDP multicast 239.0.0.1:6766 for discovery (ping/pong) and connection setup
  * the editor then connects back over TCP to us; commands + results are JSON
The editor must have:  Project Settings > Plugins > Python > "Enable Remote Execution" ticked.

Fallback when no editor is running: find the engine for a .uproject and start
UnrealEditor.exe "<project>" -ExecutePythonScript="<script>".
"""
import json, os, re, socket, subprocess, sys, time, uuid

PROTOCOL_VERSION = 1
MAGIC = "ue_py"
GROUP = ("239.0.0.1", 6766)
BIND_ADDR = "0.0.0.0"
LOOPBACK = "127.0.0.1"          # UE5 default RemoteExecutionMulticastBindAddress
COMMAND_HOST = "127.0.0.1"


def _msg(kind, source, dest=None, data=None):
    m = {"version": PROTOCOL_VERSION, "magic": MAGIC, "type": kind, "source": source}
    if dest:
        m["dest"] = dest
    if data is not None:
        m["data"] = data
    return json.dumps(m, ensure_ascii=False).encode("utf-8")


def _parse(b):
    try:
        m = json.loads(b.decode("utf-8"))
    except Exception:
        return None
    if m.get("version") != PROTOCOL_VERSION or m.get("magic") != MAGIC:
        return None
    return m


class RemoteEditor:
    """One discovered editor instance (data from its 'pong')."""
    def __init__(self, node_id, data):
        self.node_id = node_id
        self.data = data or {}

    @property
    def label(self):
        d = self.data
        proj = d.get("project_name") or "?"
        ver = d.get("engine_version") or ""
        ver = ".".join(ver.split(".")[:2]) if ver else ""
        return "%s  (UE %s)" % (proj, ver) if ver else proj

    @property
    def project_root(self):
        return self.data.get("project_root") or ""

    @property
    def project_name(self):
        return self.data.get("project_name") or ""


class RemoteExecution:
    def __init__(self, ttl=0):
        self.node_id = str(uuid.uuid4())
        self.ttl = ttl
        self.sock = None

    # ---------------- discovery
    def _open_udp(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            try:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except OSError:
                pass
        s.bind((BIND_ADDR, GROUP[1]))
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, self.ttl)
        # UE5 editors join the group on the loopback interface by default; join there (and on the
        # default interface, for editors configured with 0.0.0.0) and send via loopback.
        joined = 0
        for iface in (LOOPBACK, BIND_ADDR):
            try:
                s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                             socket.inet_aton(GROUP[0]) + socket.inet_aton(iface))
                joined += 1
            except OSError:
                pass
        if not joined:
            raise OSError("could not join multicast group %s" % GROUP[0])
        try:
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(LOOPBACK))
        except OSError:
            pass
        s.settimeout(0.1)
        self.sock = s

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    def discover(self, seconds=1.5):
        """Ping and collect pongs -> [RemoteEditor]."""
        if self.sock is None:
            self._open_udp()
        found = {}
        end = time.time() + seconds
        next_ping = 0
        while time.time() < end:
            if time.time() >= next_ping:
                self.sock.sendto(_msg("ping", self.node_id), GROUP)
                next_ping = time.time() + 0.5
            try:
                data, _ = self.sock.recvfrom(65536)
            except socket.timeout:
                continue
            except OSError:
                break
            m = _parse(data)
            if not m or m.get("source") == self.node_id:
                continue
            if m.get("type") == "pong" and m.get("dest") in (None, self.node_id):
                found[m["source"]] = RemoteEditor(m["source"], m.get("data"))
        return list(found.values())

    # ---------------- command channel
    def run(self, editor, command, mode="ExecuteStatement", unattended=False, timeout_connect=8.0):
        """Open a command connection to `editor`, run one command, return result dict.
        Blocks until the editor has finished executing (can be minutes)."""
        if self.sock is None:
            self._open_udp()
        listen = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listen.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listen.bind((COMMAND_HOST, 0))                     # any free port
        port = listen.getsockname()[1]
        listen.listen(1)
        listen.settimeout(timeout_connect)
        chan = None
        try:
            self.sock.sendto(_msg("open_connection", self.node_id, editor.node_id,
                                  {"command_ip": COMMAND_HOST, "command_port": port}), GROUP)
            try:
                chan, _ = listen.accept()
            except socket.timeout:
                raise ConnectionError("The editor didn't connect back. Is a firewall blocking Unreal on 127.0.0.1?")
            chan.settimeout(None)
            chan.sendall(_msg("command", self.node_id, editor.node_id,
                              {"command": command, "unattended": unattended, "exec_mode": mode}))
            buf = b""
            while True:
                part = chan.recv(65536)
                if not part:
                    raise ConnectionError("Editor closed the connection (did it crash?)")
                buf += part
                m = _parse(buf)
                if m is not None and m.get("type") == "command_result":
                    return m.get("data") or {}
        finally:
            try:
                self.sock.sendto(_msg("close_connection", self.node_id, editor.node_id), GROUP)
            except OSError:
                pass
            for s in (chan, listen):
                if s:
                    try:
                        s.close()
                    except OSError:
                        pass


def exec_file_statement(path, overrides=None):
    """One-line Python statement that runs `path` inside the editor (handles spaces/backslashes)."""
    g = {"__name__": "__main__", "__file__": path, "EVO_OVERRIDES": overrides or {}}
    return "exec(compile(open(%r, encoding='utf-8').read(), %r, 'exec'), %r)" % (path, path, g)


# ---------------- log tailing (live progress while the editor works)
def tail_lines(path, stop, callback, match="[EVO]"):
    """Follow `path` from its current end; call callback(line) for lines containing `match`."""
    try:
        f = open(path, "r", encoding="utf-8", errors="ignore")
    except OSError:
        return
    with f:
        f.seek(0, os.SEEK_END)
        while not stop.is_set():
            line = f.readline()
            if not line:
                time.sleep(0.25)
                continue
            if match in line:
                callback(line.rstrip())


def project_log(editor):
    root, name = editor.project_root, editor.project_name
    if root and name:
        return os.path.join(root, "Saved", "Logs", name + ".log")
    return None


# ---------------- engine discovery + launch (Windows)
def installed_engines():
    """{association: engine_root} from the Epic launcher list and the registry."""
    out = {}
    dat = os.path.join(os.environ.get("PROGRAMDATA", r"C:\ProgramData"), "Epic", "UnrealEngineLauncher", "LauncherInstalled.dat")
    try:
        with open(dat, encoding="utf-8") as f:
            for it in json.load(f).get("InstallationList", []):
                app = it.get("AppName", "")
                if app.startswith("UE_"):
                    out[app[3:]] = it.get("InstallLocation")
    except Exception:
        pass
    if sys.platform == "win32":
        try:
            import winreg
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\EpicGames\Unreal Engine") as k:
                    i = 0
                    while True:
                        try:
                            ver = winreg.EnumKey(k, i); i += 1
                        except OSError:
                            break
                        try:
                            with winreg.OpenKey(k, ver) as kv:
                                out.setdefault(ver, winreg.QueryValueEx(kv, "InstalledDirectory")[0])
                        except OSError:
                            pass
            except OSError:
                pass
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Epic Games\Unreal Engine\Builds") as k:
                    i = 0
                    while True:
                        try:
                            name, val, _ = winreg.EnumValue(k, i); i += 1
                        except OSError:
                            break
                        out.setdefault(name, val)
            except OSError:
                pass
        except ImportError:
            pass
    return {k: v for k, v in out.items() if v and os.path.isdir(v)}


def editor_exe_for_project(uproject):
    """-> (exe path or None, message)"""
    try:
        with open(uproject, encoding="utf-8") as f:
            assoc = json.load(f).get("EngineAssociation", "")
    except Exception as e:
        return None, "Can't read %s: %s" % (uproject, e)
    engines = installed_engines()
    root = engines.get(assoc)
    if not root and not assoc:      # project inside an engine source tree
        p = os.path.dirname(os.path.abspath(uproject))
        for _ in range(4):
            p = os.path.dirname(p)
            if os.path.isdir(os.path.join(p, "Engine", "Binaries")):
                root = p; break
    if not root:
        avail = ", ".join(sorted(engines)) or "none found"
        return None, "Engine '%s' for this project isn't installed (found: %s)" % (assoc or "source build", avail)
    for exe in ("UnrealEditor.exe", "UE4Editor.exe"):
        p = os.path.join(root, "Engine", "Binaries", "Win64", exe)
        if os.path.isfile(p):
            return p, "UE %s" % assoc
    return None, "No editor executable under %s" % root


REMOTE_SECTION = "[/Script/PythonScriptPlugin.PythonScriptPluginSettings]"
REMOTE_OVERRIDE = "-ini:Engine:%s:bRemoteExecution=True" % REMOTE_SECTION


def remote_execution_enabled(uproject):
    ini = os.path.join(os.path.dirname(os.path.abspath(uproject)), "Config", "DefaultEngine.ini")
    try:
        txt = open(ini, encoding="utf-8", errors="ignore").read()
    except OSError:
        return False
    sec = txt.find(REMOTE_SECTION)
    if sec < 0:
        return False
    body = txt[sec + len(REMOTE_SECTION):]
    nxt = re.search(r"^\[", body, re.M)
    body = body[:nxt.start()] if nxt else body
    return re.search(r"^\s*bRemoteExecution\s*=\s*True", body, re.M | re.I) is not None


def enable_remote_execution(uproject):
    """Turn on Project Settings > Python > Enable Remote Execution in Config/DefaultEngine.ini."""
    if remote_execution_enabled(uproject):
        return False
    ini = os.path.join(os.path.dirname(os.path.abspath(uproject)), "Config", "DefaultEngine.ini")
    os.makedirs(os.path.dirname(ini), exist_ok=True)
    try:
        with open(ini, encoding="utf-8", errors="ignore", newline="") as f:
            txt = f.read()
    except OSError:
        txt = ""
    nl = "\r\n" if "\r\n" in txt else "\n"
    txt = txt.replace("\r\n", "\n")
    if REMOTE_SECTION in txt:
        txt = re.sub(r"^\s*bRemoteExecution\s*=.*\n?", "", txt, flags=re.M | re.I)
        txt = txt.replace(REMOTE_SECTION, REMOTE_SECTION + "\nbRemoteExecution=True", 1)
    else:
        txt = txt.rstrip("\n") + "\n\n" + REMOTE_SECTION + "\nbRemoteExecution=True\n"
    with open(ini, "w", encoding="utf-8", newline="") as f:
        f.write(txt.replace("\n", nl))
    return True


def launch_editor(uproject):
    """Start the editor normally (it stays open). Remote execution is forced on via a
    command-line ini override too, in case DefaultEngine.ini couldn't be written."""
    exe, msg = editor_exe_for_project(uproject)
    if not exe:
        raise RuntimeError(msg)
    if sys.platform == "win32":
        subprocess.Popen('"%s" "%s" "%s"' % (exe, uproject, REMOTE_OVERRIDE))
    else:
        subprocess.Popen([exe, uproject, REMOTE_OVERRIDE])
    return msg


def write_runner(export_dir, overrides):
    """Write <export_dir>/_evo_send.py: sets option overrides, then runs import_into_unreal.py.
    Used both for remote execution and for -ExecutePythonScript launches."""
    target = os.path.join(export_dir, "import_into_unreal.py")
    path = os.path.join(export_dir, "_evo_send.py")
    with open(path, "w", encoding="utf-8") as f:
        f.write("# generated by EVO2UE - runs the track import with the app's options\n")
        f.write("EVO_OVERRIDES = %r\n" % (overrides or {}))
        f.write("_p = %r\n" % target)
        f.write("exec(compile(open(_p, encoding='utf-8').read(), _p, 'exec'), "
                "{'__name__': '__main__', '__file__': _p, 'EVO_OVERRIDES': EVO_OVERRIDES})\n")
    return path
