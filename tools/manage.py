#!/usr/bin/env python3
"""Install only this checkout; never copy authentication or conversation data."""
import argparse
import getpass
import hashlib
import json
import os
from pathlib import Path
import platform
import plistlib
import shlex
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
PROJECT = json.loads((ROOT / 'project.json').read_text())
SLUG = PROJECT['slug']
LABEL = 'local.' + str(os.getuid()) + '.' + SLUG

def node_runtime():
    executable = ROOT / 'runtime/node/bin/node'
    if executable.is_file():
        return executable
    if platform.system() != 'Darwin':
        raise RuntimeError('自动下载 Node.js 仅支持 macOS')
    arch = {'arm64':'arm64', 'x86_64':'x64'}.get(platform.machine())
    if not arch:
        raise RuntimeError('不支持的 CPU 架构')
    version = 'v24.22.1'
    filename = 'node-' + version + '-darwin-' + arch + '.tar.gz'
    with tempfile.TemporaryDirectory(prefix='mobile-node-') as tmp:
        archive = Path(tmp) / filename
        sums = Path(tmp) / 'SHASUMS256.txt'
        base = 'https://nodejs.org/dist/' + version + '/'
        subprocess.run(['curl','-fL','--retry','3','-o',str(archive),base+filename],check=True)
        subprocess.run(['curl','-fsSL','--retry','3','-o',str(sums),base+'SHASUMS256.txt'],check=True)
        expected = next(line.split()[0] for line in sums.read_text().splitlines() if line.split()[-1] == filename)
        digest = hashlib.sha256()
        with archive.open('rb') as source:
            for chunk in iter(lambda: source.read(1024*1024), b''):
                digest.update(chunk)
        if digest.hexdigest() != expected:
            raise RuntimeError('Node.js SHA256 校验失败')
        runtime = ROOT / 'runtime'
        runtime.mkdir(exist_ok=True)
        subprocess.run(['tar','-xzf',str(archive),'-C',str(runtime)],check=True)
        link = runtime / 'node'
        if link.is_symlink():
            link.unlink()
        link.symlink_to('node-' + version + '-darwin-' + arch)
    return executable

def prepare_files():
    config = ROOT / 'config.json'
    if not config.exists():
        config.write_bytes((ROOT / 'config.example.json').read_bytes())
        config.chmod(0o600)
    (ROOT / 'logs').mkdir(exist_ok=True)
    subprocess.run([sys.executable,str(ROOT/'tools/make_icons.py')],check=True)

def owned_plist(file):
    try:
        with file.open('rb') as source:
            return plistlib.load(source).get('WorkingDirectory') == str(ROOT)
    except (OSError, ValueError, plistlib.InvalidFileException):
        return False

def service_definition(command):
    return {'Label':LABEL,'ProgramArguments':command,'WorkingDirectory':str(ROOT),
      'EnvironmentVariables':{'HOME':str(Path.home()),'LANG':'zh_CN.UTF-8','PATH':str(ROOT/'runtime/node/bin')+':/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin'},
      'RunAtLoad':True,'KeepAlive':True,'ThrottleInterval':10,
      'StandardOutPath':str(ROOT/'logs/launchd.log'),'StandardErrorPath':str(ROOT/'logs/launchd.log')}

def shortcut_command():
    if SLUG == 'codex-mobile':
        return [sys.executable,str(ROOT/'connect.py')]
    return [sys.executable,str(ROOT/'attach.py'),'url','--copy']

def install(no_start=False):
    if platform.system() != 'Darwin':
        raise RuntimeError('安装脚本只支持 macOS')
    prepare_files()
    if SLUG == 'codex-mobile':
        command = [str(node_runtime()),str(ROOT/'server.mjs')]
    else:
        command = [sys.executable,str(ROOT/'attach.py'),'serve']
    desktop = Path.home()/'Desktop'
    desktop.mkdir(exist_ok=True)
    shortcut = desktop/(PROJECT['title']+'手机连接.command')
    if shortcut.exists() and str(ROOT) not in shortcut.read_text(errors='replace'):
        raise RuntimeError('桌面已有其他安装的同名连接工具，请先重命名或移走')
    shortcut.write_text('#!/bin/bash\nexec '+shlex.join(shortcut_command())+'\n')
    shortcut.chmod(0o755)
    if no_start:
        print('文件和连接工具已准备；未注册或启动登录服务。')
        return
    target = Path.home()/'Library/LaunchAgents'/(LABEL+'.plist')
    if target.exists() and not owned_plist(target):
        raise RuntimeError('存在其他安装的服务，请先在旧目录卸载')
    if target.exists():
        subprocess.run(['launchctl','bootout','gui/'+str(os.getuid()),str(target)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    target.parent.mkdir(parents=True,exist_ok=True)
    with target.open('wb') as out:
        plistlib.dump(service_definition(command),out)
    target.chmod(0o600)
    subprocess.run(['launchctl','bootstrap','gui/'+str(os.getuid()),str(target)],check=True)
    print('登录服务已注册。请打开对应桌面应用，再双击桌面的连接工具。')
    print('未就绪时查看本目录 logs/launchd.log；已有旧服务占用端口时需先停止旧服务。')

def uninstall():
    target = Path.home()/'Library/LaunchAgents'/(LABEL+'.plist')
    if owned_plist(target):
        subprocess.run(['launchctl','bootout','gui/'+str(os.getuid()),str(target)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        target.unlink()
    shortcut = Path.home()/'Desktop'/(PROJECT['title']+'手机连接.command')
    if shortcut.exists() and str(ROOT) in shortcut.read_text(errors='replace'):
        shortcut.unlink()
    print('本安装的服务和连接工具已移除，程序、凭据及用户数据均保留。')

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command',choices=['install','uninstall','prepare'])
    parser.add_argument('--no-start',action='store_true')
    args = parser.parse_args()
    try:
        if args.command == 'prepare': prepare_files()
        elif args.command == 'install': install(args.no_start or os.environ.get('INSTALL_NO_START') == '1')
        else: uninstall()
    except Exception:
        print('操作未完成。请检查 Python/Node、网络、文件权限或端口冲突；不会打印配置或配对链接。',file=sys.stderr)
        return 1
    return 0
if __name__ == '__main__':
    sys.exit(main())
