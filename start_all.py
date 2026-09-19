# -*- coding: utf-8 -*-
"""一键启动 6 个作品集 Demo 服务。

用法:
    G:/code/hermes_project/.venv/Scripts/python.exe start_all.py          # 启动全部(已在跑的跳过)
    G:/code/hermes_project/.venv/Scripts/python.exe start_all.py 1 2 6    # 只启动 1/2/6
    G:/code/hermes_project/.venv/Scripts/python.exe start_all.py --stop   # 全部停止
    G:/code/hermes_project/.venv/Scripts/python.exe start_all.py --status # 健康检查
"""
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
PY = os.path.join(BASE, '.venv', 'Scripts', 'python.exe')
if not os.path.exists(PY):
    PY = sys.executable

PROJECTS = [
    (1, 'proj1-library', 8001, '图书借阅系统'),
    (2, 'proj2-mall', 8002, '网上商城'),
    (3, 'proj3-inventory-logistics', 8003, '进销存 + 宅急送物流'),
    (4, 'proj4-ledger-bi', 8004, '记账 + 数据看板'),
    (5, 'proj5-saas', 8005, '多租户 SaaS 平台'),
    (6, 'proj6-ai-agent', 8006, 'AI 商品推荐助手'),
]


def _run(cmd):
    """执行命令, 返回 (rc, stdout)。stdout 可能为空(命令无输出)。"""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=25)
        return p.returncode, (p.stdout or '') + (p.stderr or '')
    except Exception as exc:  # pragma: no cover
        return -1, str(exc)


def port_pid(port):
    """返回监听该端口的 PID, 没有则 None。用 netstat + tasklist 判定, 不依赖第三方库。"""
    rc, out = _run(['netstat', '-ano'])
    if rc != 0:
        return None
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[3] == 'LISTENING' and parts[1].endswith(':' + str(port)):
            return parts[4]
    return None


def healthy(port, timeout=3):
    """GET /login, 2xx/3xx 视为存活。"""
    url = 'http://127.0.0.1:%d/login' % port
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'start-all/1.0'})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 400
    except urllib.error.HTTPError as exc:
        return 200 <= exc.code < 400
    except Exception:
        return False


def stop_all(quiet=False):
    killed = 0
    for num, slug, port, name in PROJECTS:
        pid = port_pid(port)
        if pid:
            _run(['taskkill', '/F', '/PID', str(pid)])
            killed += 1
            if not quiet:
                print('  停止 %-26s :%d (PID %s)' % (name, port, pid))
    print('已停止 %d 个服务' % killed)


def start_one(num, slug, port, name, quiet=False):
    if port_pid(port):
        if healthy(port):
            print('  [跳过] %-26s :%d 已在运行' % (name, port))
            return True
        print('  [警告] %-26s :%d 被占用但无响应, 先清理' % (name, port))
        _run(['taskkill', '/F', '/PID', str(port_pid(port))])
        time.sleep(1)
    pdir = os.path.join(BASE, slug)
    if not os.path.isdir(pdir):
        print('  [错误] 目录不存在: %s' % pdir)
        return False
    log = open(os.path.join(pdir, '_svc.log'), 'ab')
    kwargs = {}
    if os.name == 'nt':
        kwargs['creationflags'] = 0x00000008 | 0x00000200  # DETACHED_PROCESS|NEW_PROCESS_GROUP
    subprocess.Popen([PY, 'app.py'], cwd=pdir, stdout=log, stderr=log,
                     stdin=subprocess.DEVNULL, **kwargs)
    for _ in range(20):
        time.sleep(0.5)
        if healthy(port):
            print('  [启动] %-26s http://127.0.0.1:%d' % (name, port))
            return True
    print('  [失败] %-26s :%d 未在 10s 内就绪, 见 %s/_svc.log' % (name, port, slug))
    return False


def main():
    args = [a for a in sys.argv[1:]]
    if '--stop' in args:
        print('停止全部服务...')
        stop_all()
        return 0
    if '--status' in args:
        print('服务健康检查:')
        bad = 0
        for num, slug, port, name in PROJECTS:
            ok = healthy(port)
            pid = port_pid(port)
            print('  %s %-26s :%d  %s' % ('[OK]  ' if ok else '[DOWN]', name, port,
                                          ('PID ' + pid) if pid else '未监听'))
            bad += 0 if ok else 1
        print('结论: %d/%d 健康' % (len(PROJECTS) - bad, len(PROJECTS)))
        return 0 if bad == 0 else 1

    wanted = set()
    for a in args:
        if a.isdigit():
            wanted.add(int(a))
    targets = [p for p in PROJECTS if not wanted or p[0] in wanted]
    print('启动 %d 个服务 (Python: %s)' % (len(targets), PY))
    ok = 0
    for num, slug, port, name in targets:
        ok += 1 if start_one(num, slug, port, name) else 0
    print('结果: %d/%d 已就绪' % (ok, len(targets)))
    if ok:
        print('登录账号: admin/admin123 (管理员)  ops/ops123 (操作员)  demo/demo123 (普通用户)')
        print('批量自测: %s verify_all.py' % PY)
    return 0 if ok == len(targets) else 1


if __name__ == '__main__':
    sys.exit(main())
