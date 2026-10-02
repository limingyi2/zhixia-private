# -*- coding: utf-8 -*-
"""把新 APK 发布到云端，手机上点"检查更新"就能收到。

沿用 persync 的分块传输（payload 进命令行会超 Windows 32K 上限），
流程是：备份旧包 -> 写新包 -> 抬 version.json 的 code -> 回读校验 sha256。

校验不能省：`wrote N bytes` 来自云端进程自己，它认为成功就成了 ——
persync 那次就是这么把包写到了错的目录、报成功却没生效。

    python tools/publish_apk.py            # 发布，code 自动 +1
    python tools/publish_apk.py 4.9        # 指定版本名
"""
import base64
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import persync as ps

APK = r"F:\Vigil\deploy\zhixia.apk"
CLOUD_APP = "C:/Vigil/app"
STEP = 3000


def _cur_ver():
    out = ps._cloud_run(
        "import json,os;v=json.load(open('%s/version.json',encoding='utf-8'));"
        "print(v['code'],v['name'])" % CLOUD_APP)
    parts = (out or "").split()
    return int(parts[0]), (parts[1] if len(parts) > 1 else "")


def _send(apk_path):
    """把 APK 分块推到云端。

    中间文件 _apk.b64 落的是 **base64 文本本身**，每块直接追加、不解码 ——
    早先写成"每块 b64decode 再追加"，等于把整串解了两遍：云端文件大小会等于
    APK 原始大小而不是 base64 长度，而且每块各自补位还会吃掉字符
    （127 块就少了 3 个），最后 sha256 对不上。
    """
    data = base64.b64encode(open(apk_path, "rb").read()).decode()
    tmp = "C:/Vigil/_apk.b64"
    ps._cloud_run("open('%s','wb').close()" % tmp)
    for i in range(0, len(data), STEP):
        # 纯文本追加，不解码。分段只是为了绕开命令行长度上限
        out = ps._cloud_run(
            "open('%s','ab').write(b'%s');print('ok')" % (tmp, data[i:i + STEP]))
        if "ok" not in (out or ""):
            raise SystemExit("传到第 %d 块失败：%r" % (i // STEP, out))
    got = ps._cloud_run("import os;print(os.path.getsize('%s'))" % tmp)
    if got.strip() != str(len(data)):
        raise SystemExit("传完了但长度不对：云端 %s / 本地 %d" % (got.strip(), len(data)))
    ps._cloud_run(
        "import base64,os;open('%s/zhixia.apk.new','wb').write("
        "base64.b64decode(open('%s','rb').read()));print('decoded')"
        % (CLOUD_APP, tmp))


def main():
    if not os.path.isfile(APK):
        raise SystemExit("没找到 %s，先跑 deploy/rebuild_apk.py" % APK)
    raw = open(APK, "rb").read()
    sha = hashlib.sha256(raw).hexdigest()
    old_code, _ = _cur_ver()
    new_code = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() \
        else old_code + 1
    name = sys.argv[2] if len(sys.argv) > 2 else "v%s" % (
        "4.%d" % (new_code - 17) if new_code > 17 else str(new_code))
    note = sys.argv[3] if len(sys.argv) > 3 else ""

    print("APK      = %s" % APK)
    print("大小     = %d bytes" % len(raw))
    print("sha256   = %s" % sha)
    print("版本     = code %d -> %d（%s）" % (old_code, new_code, name))

    print("\n1. 备份旧的")
    ps._cloud_run("import shutil,os;p='%s';"
                  "[shutil.copyfile(p+'/'+f, p+'/'+f+'.bak_v%d') "
                  "for f in ('zhixia.apk','version.json') "
                  "if os.path.exists(p+'/'+f)]" % (CLOUD_APP, old_code))

    print("2. 传包（分块）")
    _send(APK)

    print("3. 换包并抬版本号")
    ps._cloud_run("import os,shutil;shutil.move('%s/zhixia.apk.new','%s/zhixia.apk')"
                  % (CLOUD_APP, CLOUD_APP))
    ver = {"code": new_code, "name": name, "url": "/api/app.apk"}
    if note:
        ver["note"] = note
    # 配置走 base64：json 里的双引号会被 Windows 命令行吃掉，note 里的中文标点
    # 还会连带乱码。编码一次就都躲开（persync 踩过同一类坑）
    vb64 = base64.b64encode(json.dumps(ver, ensure_ascii=False)
                            .encode("utf-8")).decode()
    ps._cloud_run("import base64,json;json.loads(base64.b64decode('%s'));"
                  "open('%s/version.json','wb').write(base64.b64decode('%s'));"
                  "print('ver ok')" % (vb64, CLOUD_APP, vb64))

    print("4. 回读校验")
    out = ps._cloud_run(
        "import hashlib,os,json;"
        "p='%s/zhixia.apk';"
        "print('size',os.path.getsize(p));"
        "print('sha',hashlib.sha256(open(p,'rb').read()).hexdigest());"
        "print('ver',json.load(open('%s/version.json',encoding='utf-8'))['code'])"
        % (CLOUD_APP, CLOUD_APP))
    got = {}
    for line in (out or "").splitlines():
        p = line.split()
        if len(p) == 2:
            got[p[0]] = p[1]
    ok = got.get("sha") == sha
    print("   云端 sha = %s" % got.get("sha"))
    print("   一致     = %s" % ("是" if ok else "否 ← 不一致，别让手机去下"))
    if not ok:
        raise SystemExit("校验失败：云端上的包和本地不是同一个")
    print("\n发布完成。手机上「检查更新」会提示 v%s。" % name)


if __name__ == "__main__":
    main()
