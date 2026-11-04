#!/usr/bin/env python3
"""Run as root on the server to create/replace the diary password hash."""
import getpass,hashlib,json,os,pwd,secrets,tempfile
from pathlib import Path
password=getpass.getpass('Новый пароль дневника (минимум 12 символов): ')
if len(password)<12: raise SystemExit('Нужно минимум 12 символов.')
if password!=getpass.getpass('Повторите пароль: '): raise SystemExit('Пароли не совпадают.')
p=Path('/etc/golova/config.json');p.parent.mkdir(mode=0o750,parents=True,exist_ok=True)
salt=secrets.token_bytes(16)
content=json.dumps({'salt':salt.hex(),'password_hash':hashlib.pbkdf2_hmac('sha256',password.encode(),salt,300000).hex()})
fd,name=tempfile.mkstemp(dir=p.parent)
try:
 with os.fdopen(fd,'w') as f:f.write(content)
 os.chmod(name,0o640);os.chown(name,0,pwd.getpwnam('golova').pw_gid);os.replace(name,p)
finally:
 if os.path.exists(name):os.unlink(name)
print('Пароль сохранён. Перезапустите golova.service, чтобы завершить старые сеансы.')
