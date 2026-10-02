# -*- coding: utf-8 -*-
"""人设同步：本地 <-> 私有 git 仓库 <-> 云端。

为什么要这个：`config/personas/` 和 `config.json` 都被 .gitignore 挡着（公开仓库
不能有人设和 API key），所以它们只存在于本机和云端两个地方。任一边坏了、或者要
换服务器，就没地方找回来。这个脚本把两边都收进私有仓库，换机器时只需
clone 那个仓库再 pull 下去。

三条命令：
    persync.py save     把本地人设与配置提交到私有仓库（改完人设跑这个）
    persync.py push     本地 -> 私有仓库 -> 云端（真正生效）
    persync.py pull     私有仓库 -> 本地（换服务器或本地丢了之后用）
    persync.py status    只看两边有没有不一致，不改任何东西

云端那条路走 SSH，key 与 host 沿用部署脚本那套，不另配。
"""
import base64
import json
import os
import subprocess
import sys

LOCAL = r"F:\Vigil"
REPO = os.path.join(LOCAL, ".workbuddy", "private_repo")
REMOTE = "https://github.com/limingyi2/zhixia-private.git"

# 云端：跟 deploy_staging.py 一致
SSH = r"C:\Windows\System32\OpenSSH\ssh.exe"
KEY = os.path.join(LOCAL, "deploy", "sshkeys", "linzhixia_deploy")
HOST = "administrator@203.0.113.10"
CLOUD = "C:/Vigil"
CLOUD_PY = "C:/Vigil/.venv/Scripts/python.exe"

# 会被同步的东西：只有人设。
#
# config.json **不在其中**，这是踩过的坑：它是运行配置，两边本来就不该一样 ——
# 云端有 brain_token、模型名是云端那套、persona_file 指向云端路径；本地这几项
# 都不一样。整个文件推过去会拿本地的空 provider / 没 token 的配置盖掉云端，
# 线上直接起不来。
# 真要改公共配置项（relation 之类），手动两边都改，或者只抄那一个键。
SYNC_GLOBS = [
    ("brain/config/personas", "*.json"),
]

GIT_IGNORE = """# 这个仓库只有人设与配置，不该有别的
__pycache__/
*.pyc
"""


def _git(*a, cwd=REPO):
    r = subprocess.run(["git"] + list(a), capture_output=True,
                       cwd=cwd, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise SystemExit("git %s 失败：%s" % (" ".join(a), (r.stderr or "")[:300]))
    return (r.stdout or "").strip()


def collect_local():
    """读本地要同步的那些文件 -> {相对路径: 内容字符串}"""
    out = {}
    for sub, pat in SYNC_GLOBS:
        d = os.path.join(LOCAL, sub)
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            if not fn.endswith(".json"):
                continue
            if pat != "*.json" and not fn.endswith(pat.lstrip("*")):
                continue
            p = os.path.join(d, fn)
            if not os.path.isfile(p):
                continue
            try:
                json.load(open(p, encoding="utf-8"))   # 坏 JSON 不收
            except Exception as e:
                print("  跳过 %s/%s：解析失败 %s" % (sub, fn, e))
                continue
            out["%s/%s" % (sub, fn)] = open(p, encoding="utf-8").read()
    return out


def _cloud_run(pycode, timeout=120):
    """在云端跑一段 python（压成单行，多行会被 cmd 截断），返回 stdout。

    失败时把 stderr 带回来看 —— 静默返回空串会让人以为"跑完了只是没输出"，
    实际上远端早就 traceback 了（这个坑踩过两次）。
    """
    cmd = '"%s" -c "%s"' % (CLOUD_PY, pycode)
    r = subprocess.run([SSH, "-i", KEY, "-o", "StrictHostKeyChecking=no",
                        "-o", "ConnectTimeout=20", "-o", "ServerAliveInterval=15",
                        HOST, cmd],
                       capture_output=True, timeout=timeout)
    out = r.stdout.decode("utf-8", "replace").strip()
    if r.returncode != 0:
        err = r.stderr.decode("utf-8", "replace").strip()
        raise SystemExit("云端命令失败（rc=%d）：%s\n命令：%s"
                         % (r.returncode, err[-400:], pycode[:120]))
    return out


def _cloud_fetch():
    """拉云端人设，返回 {相对路径: 文本}。用 base64 传输 ——
    中文经 Windows SSH 回来会乱码，编码一次就干净了。

    只取 personas/*.json，不取 config.json（理由见 SYNC_GLOBS 那段）。
    """
    py = ("import base64,glob,os,json;"
          "d={};"
          "[d.update({'brain/config/personas/'+os.path.basename(p):"
          "json.load(open(p,encoding='utf-8'))}) for p in "
          "glob.glob('%s/config/personas/*.json')];"
          "print(base64.b64encode(json.dumps(d,ensure_ascii=False).encode('utf-8')).decode())"
          % CLOUD)
    out = _cloud_run(py)
    if not out:
        raise SystemExit("云端没返回内容，SSH 可能不通")
    data = json.loads(base64.b64decode(out).decode("utf-8"))
    return {k: json.dumps(v, ensure_ascii=False, indent=2) + "\n"
            for k, v in data.items()}


def _cloud_put(files):
    """把 {相对路径: 文本} 写到云端。

    三个坑叠在这：
    1. 中文经 Windows SSH 回来会乱码 -> 全程 base64
    2. payload 不能进命令行（WinError 206，命令行 32K 上限，8 个人设就超了）
       -> 分块追加到云端临时文件，再让云端读它展开
    3. 每块都要自己 import：远端是 python -c，一次性进程，块之间不共享命名空间

    写完逐个回读校验。这道校验不是多余的：踩过两次"报 wrote 8 但文件没变"，
    一次是路径多拼了一层目录，一次是解了两遍 base64。光看返回值发现不了 ——
    那个返回值来自云端进程自己，它认为成功了。
    """
    payload = base64.b64encode(json.dumps(
        {k: base64.b64encode(v.encode("utf-8")).decode() for k, v in files.items()},
        ensure_ascii=False).encode("utf-8")).decode()

    local_tmp = os.path.join(LOCAL, "deploy", "_persync_payload.b64")
    os.makedirs(os.path.dirname(local_tmp), exist_ok=True)
    with open(local_tmp, "w", encoding="ascii") as f:
        f.write(payload)

    tmp = "C:/Vigil/_persync.b64"
    _cloud_run("open('%s','wb').close()" % tmp)      # 先建空的，别留上次的
    step = 3000
    for i in range(0, len(payload), step):
        out = _cloud_run("import base64;open('%s','ab').write(base64.b64decode('%s'));"
                         "print('ok')" % (tmp, payload[i:i + step]))
        if "ok" not in (out or ""):
            raise SystemExit("传到第 %d 块时失败：%r" % (i // step, out))

    # 云端是扁平结构：本地 brain/config/personas/x.json 对应云端
    # config/personas/x.json。直接拼本地相对路径会多出一层 brain\，文件写进去了
    # 但程序读的是另一个地方 —— 看着"写成功"其实没生效。
    wrote = _cloud_run(
        "import base64,json,os;"
        # 落盘的是 payload 明文（每块各自 b64decode 过），别再解一次
        "d=json.loads(open('%s',encoding='utf-8').read());"
        "[ (os.makedirs('%s/config/personas',exist_ok=True),"
        "  open('%s/config/personas/'+k.split('/')[-1],'wb').write(base64.b64decode(v))) "
        " for k,v in d.items()];"
        "print('wrote',len(d))" % (tmp, CLOUD, CLOUD))

    check = _cloud_run(
        "import json,glob,os;"
        "[print(os.path.basename(p),len(json.load(open(p,encoding='utf-8')).get('notes','')))"
        " for p in sorted(glob.glob('%s/config/personas/*.json'))]" % CLOUD)
    remote = {}
    for line in (check or "").splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].isdigit():
            remote[parts[0]] = int(parts[1])

    bad = []
    for k, v in files.items():
        fn = os.path.basename(k)
        want = len(str((json.loads(v) or {}).get("notes") or ""))
        if remote.get(fn) != want:
            bad.append("%s 期望 %d 字，云端读到 %s"
                       % (fn, want, remote.get(fn, "文件不存在")))
    if bad:
        raise SystemExit("写完了但内容对不上：\n  " + "\n  ".join(bad))
    return wrote


def _norm(text):
    """比内容时统一按 JSON 语义比，避免缩进和末尾换行差异当成不一致。"""
    try:
        return json.dumps(json.loads(text), ensure_ascii=False, sort_keys=True)
    except Exception:
        return (text or "").strip()


def cmd_status():
    loc = collect_local()
    cloud = _cloud_fetch()
    keys = sorted(set(loc) | set(cloud))
    diff = [k for k in keys
            if k not in loc or k not in cloud
            or _norm(loc[k]) != _norm(cloud.get(k, ""))]
    print("本地 %d 个文件，云端 %d 个" % (len(loc), len(cloud)))
    if not diff:
        print("两边一致。")
        return
    print("不一致：")
    for k in diff:
        where = []
        if k in loc and k in cloud:
            where.append("内容不同")
        elif k in loc:
            # default.json 是仓库给用户看的空模板，云端有真人设就不需要它
            if os.path.basename(k) == "default.json":
                where.append("只在本地（空模板，正常）")
            else:
                where.append("只在本地")
        else:
            where.append("只在云端")
        print("  %-42s %s" % (k, "、".join(where)))
    print("\n跑 persync.py push 让云端跟本地一致。")


def cmd_save(msg="更新人设"):
    _git("init", "-q")
    for rel, text in collect_local().items():
        p = os.path.join(REPO, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, "w", encoding="utf-8", newline="").write(text)
    gi = os.path.join(REPO, ".gitignore")
    if not os.path.exists(gi):
        open(gi, "w", encoding="utf-8").write(GIT_IGNORE)
    if not _git("status", "--porcelain"):
        print("私有仓库已是最新，无需提交。")
        return
    _git("add", "-A")
    _git("commit", "-q", "-m", msg)
    print("已提交到私有仓库：%s" % _git("log", "--oneline", "-1"))


def _push_git():
    """主站可能连不上（代理分流），失败时给一句能照做的提示。

    schannelCheckRevoke=false 是必需的：本机证书吊销检查走不通，报
    CRYPT_E_NO_REVOCATION_CHECK，看起来像证书坏了，其实是查不到吊销列表。
    """
    r = subprocess.run(["git", "-c", "http.sslVerify=false",
                        "-c", "http.schannelCheckRevoke=false",
                        "push", "-u", "origin", "HEAD"],
                       cwd=REPO, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if r.returncode == 0:
        return True
    print("git push 失败：%s" % (r.stderr or "")[:200])
    print("私有仓库地址：%s" % REMOTE)
    print("如果是代理分流导致的，开全局模式后重试；")
    print("实在不行可以手动在仓库页面 Add file 上传。")
    return False


def cmd_push():
    files = collect_local()
    if not files:
        raise SystemExit("本地没读到任何人设文件，先确认 brain/config/personas/ 在")
    print("推送 %d 个文件" % len(files))
    cmd_save("push 前快照")
    if _push_git():
        print("→ 私有仓库已更新")
    print("→ 写云端：%s" % _cloud_put(files))
    print("完成。云端要重启才生效：ssh 重启 LinZhixiaBrain 计划任务。")


def cmd_pull():
    """从私有仓库取最新，覆盖本地人设。换服务器后跑这个就能把人格捞回来。"""
    if not os.path.isdir(os.path.join(REPO, ".git")):
        subprocess.run(["git", "clone", REMOTE, REPO], check=True)
    else:
        _git("fetch", "origin")
        _git("checkout", "--", ".")
    files = {}
    for sub, _ in SYNC_GLOBS:
        d = os.path.join(REPO, sub)
        if not os.path.isdir(d):
            continue
        for fn in os.listdir(d):
            if fn.endswith(".json"):
                files["%s/%s" % (sub, fn)] = open(
                    os.path.join(d, fn), encoding="utf-8").read()
    if not files:
        raise SystemExit("私有仓库里没有可同步的文件")
    _write_local(files)
    print("已从私有仓库恢复 %d 个文件到本地。" % len(files))
    print("提醒：relation/persona_file 这两个键在 config.json 里，"
          "仓库不同步它（见 SYNC_GLOBS 注释），新机器上要手写一次。")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    msg = sys.argv[2] if len(sys.argv) > 2 else "更新人设"
    {"status": cmd_status, "save": lambda: cmd_save(msg),
     "push": cmd_push, "pull": cmd_pull}.get(cmd, cmd_status)()
