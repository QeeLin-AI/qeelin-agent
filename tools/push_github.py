import sys, os, urllib.request, urllib.error, json, base64

REPO = "QeeLin-AI/qeelin-agent"
ROOT = r"D:\Programs\DSH\crypto-agent"
BASE = f"https://api.github.com/repos/{REPO}/git"

token = None
with open(f"{ROOT}/.env", encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if line.startswith("GITHUB_TOKEN"):
            token = line.split("=", 1)[1].strip().strip('"').strip("'")
            break
assert token, "GITHUB_TOKEN not found"

def api(path, method="GET", data=None):
    url = BASE + path
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "cryptoagent-deploy",
    }
    body = json.dumps(data).encode("utf-8") if data is not None else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        print("HTTPError", e.code, e.read().decode("utf-8")[:800])
        raise

st, js = api("/ref/heads/main")
head = js["object"]["sha"]
st, js = api(f"/commits/{head}")
base_tree = js["tree"]["sha"]
print("base", head, base_tree)

files = sys.argv[1:] or [
    "src/cryptoagent/web/static/index.html",
    "src/cryptoagent/web/static/app.js",
    "src/cryptoagent/web/server.py",
]

blobs = []
for p in files:
    # 以 - 开头表示删除该路径（重命名/清理时用）。Git Data API 里
    # tree 条目 sha=None 即代表删除；只传新文件不传删除，旧文件会永远留在仓库里。
    if p.startswith("-"):
        path = p[1:]
        blobs.append({"path": path, "mode": "100644", "type": "blob", "sha": None})
        print("delete", path)
        continue
    with open(f"{ROOT}/{p}", "rb") as f:
        content = base64.b64encode(f.read()).decode("ascii")
    st, js = api("/blobs", "POST", {"content": content, "encoding": "base64"})
    print("blob", p, js["sha"])
    blobs.append({"path": p, "mode": "100644", "type": "blob", "sha": js["sha"]})

st, js = api("/trees", "POST", {"base_tree": base_tree, "tree": blobs})
new_tree = js["sha"]
print("tree", new_tree)

msg = os.environ.get("GIT_MSG") or "feat(ui): 持仓期间在总览「持仓」卡显示持仓管理员巡检状态——last_management 带时间戳，页面可判断守护线程是否仍在工作（原先 hold 时整张管理员卡片隐藏，看不出它有没有跑）"
st, js = api("/commits", "POST", {"message": msg, "tree": new_tree, "parents": [head]})
commit = js["sha"]
print("commit", commit)

st, js = api("/refs/heads/main", "PATCH", {"sha": commit, "force": False})
print("ref", js.get("ref"), js.get("object", {}).get("sha"))
print("DONE", commit)
