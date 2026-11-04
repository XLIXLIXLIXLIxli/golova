#!/usr/bin/env python3
"""Private headache journal; Google Sheets remains the source of truth."""
import base64, hashlib, hmac, json, os, secrets, threading, time
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, urlparse
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
CONFIG = Path(os.environ.get('GOLOVA_CONFIG', '/etc/golova/config.json'))
KEY = Path(os.environ.get('GOOGLE_APPLICATION_CREDENTIALS', '/etc/golova/service-account.json'))
SHEET_ID = os.environ.get('GOOGLE_SHEET_ID', '')
ORIGINS = {value.strip() for value in os.environ.get('GOLOVA_ORIGINS', 'https://bar27.ru,https://www.bar27.ru').split(',') if value.strip()}
HEADERS = ['Отметка времени','Дата','Время','Симптом','Сила','Где болит','Прошла','Длилась','Таблетка','Температура, °C','Давление, мм рт. ст.','Изм. давления за 3 ч','Изм. давления за 24 ч','Влажность, %','Погода','Пульс','Вариабельность пульса, мс','Пульс в покое','Примечание']
LOCK = threading.RLock()
FAILURES = {}
SESSIONS = {}
CACHE = {'time': 0, 'data': None}

class APIError(Exception):
    def __init__(self, message, status=400): self.status, self.message = status, message

def config():
    if not CONFIG.exists(): raise APIError('Вход ещё не настроен администратором.',503)
    return json.loads(CONFIG.read_text())

def google(method, suffix='', **kwargs):
    if not SHEET_ID: raise APIError('Не задан GOOGLE_SHEET_ID на сервере.',503)
    if not KEY.exists(): raise APIError('Подключение Google Sheets ещё не настроено. Нужен сервисный аккаунт.',503)
    from google.oauth2.service_account import Credentials
    from google.auth.transport.requests import AuthorizedSession
    creds = Credentials.from_service_account_file(str(KEY), scopes=['https://www.googleapis.com/auth/spreadsheets'])
    with AuthorizedSession(creds) as session:
        response = session.request(method, 'https://sheets.googleapis.com/v4/spreadsheets/'+SHEET_ID+suffix, timeout=25, **kwargs)
    if not response.ok:
        if response.status_code in (401,403): raise APIError('Google отклонил доступ. Проверьте Sheets API и роль «Редактор» для сервисного аккаунта в таблице.',502)
        raise APIError('Не удалось обратиться к Google Sheets (код '+str(response.status_code)+'). Повторите позже.',502)
    return response.json()

def parse_date(value, tz):
    if isinstance(value, (int,float)) and not isinstance(value,bool):
        return (datetime(1899,12,30)+timedelta(days=value)).replace(tzinfo=tz)
    s=str(value).strip()
    if not s: return None
    for f in ('%d.%m.%Y %H:%M:%S','%d.%m.%Y %H:%M','%d.%m.%Y','%Y-%m-%d %H:%M:%S','%Y-%m-%d %H:%M'):
        try: return datetime.strptime(s,f).replace(tzinfo=tz)
        except ValueError: pass
    try:
        d=datetime.fromisoformat(s.replace('Z','+00:00'))
        return d.replace(tzinfo=tz) if d.tzinfo is None else d.astimezone(tz)
    except ValueError: return None

def convert(values, tz):
    if not values: return []
    heads=values[0]; result=[]
    for rownum,row in enumerate(values[1:],2):
        r={str(h):row[i] if i<len(row) else '' for i,h in enumerate(heads)}
        start=parse_date(r.get('Отметка времени',''),tz)
        if not start: start=parse_date(str(r.get('Дата',''))+' '+str(r.get('Время','')),tz)
        if not start: continue
        end=parse_date(r.get('Прошла',''),tz)
        r.update(row=rownum,startedAt=start.astimezone(timezone.utc).isoformat(),day=start.date().isoformat(),durationMinutes=None)
        if end and end>=start: r['durationMinutes']=round((end-start).total_seconds()/60)
        result.append(r)
    return result

def get_data(force=False):
    with LOCK:
        if not force and CACHE['data'] is not None and time.monotonic()-CACHE['time']<15: return CACHE['data']
        meta=google('GET', params={'fields':'properties(timeZone),sheets(properties(sheetId,title))'})
        tzname=meta['properties']['timeZone']; tz=ZoneInfo(tzname)
        sheets={s['properties']['title']:s['properties']['sheetId'] for s in meta.get('sheets',[])}
        if 'Головная боль' not in sheets: raise APIError('В таблице не найден лист «Головная боль».',502)
        names=[n for n in ('Головная боль','Таблетки') if n in sheets]
        raw=google('GET','/values:batchGet',params={'ranges':["'"+n+"'!A:S" for n in names], 'valueRenderOption':'FORMATTED_VALUE'})
        lists={n:convert(v.get('values',[]),tz) for n,v in zip(names,raw.get('valueRanges',[]))}
        data={'headaches':lists.get('Головная боль',[]),'pills':lists.get('Таблетки',[]),'timezone':tzname,'today':datetime.now(tz).date().isoformat(),'updatedAt':datetime.now(timezone.utc).isoformat()}
        CACHE.update(time=time.monotonic(),data=data, sheets=sheets)
        return data

def text_input(body,key,limit=1000):
    value=body.get(key,'')
    if not isinstance(value,str): raise APIError('Некорректное поле '+key)
    value=value.strip()
    if len(value)>limit: raise APIError('Слишком длинное поле '+key)
    return value

def cell(value, date=False):
    c={'userEnteredValue':{'numberValue':value} if isinstance(value,(int,float)) else {'stringValue':str(value)}}
    if date: c['userEnteredFormat']={'numberFormat':{'type':'DATE_TIME','pattern':'dd.MM.yyyy HH:mm:ss'}}
    return c

def write_action(body):
    action=body.get('action')
    if action not in ('start','end','pill'): raise APIError('Неизвестное действие.')
    with LOCK:
        data=get_data(force=True); tz=ZoneInfo(data['timezone']); now=datetime.now(tz)
        sheets=CACHE['sheets']; requests=[]
        if action=='start':
            severity=text_input(body,'severity',50) or 'не сказал'
            if severity not in ('Слабая','Умеренная','Сильная','не сказал'): raise APIError('Выберите силу боли.')
            location=text_input(body,'location',150) or 'не сказал'; note=text_input(body,'note') or 'не сказал'
            serial=(now.replace(tzinfo=None)-datetime(1899,12,30)).total_seconds()/86400
            values=[serial,now.strftime('%d.%m.%Y'),now.strftime('%H:%M'),'Головная боль',severity,location,'','','']+['нет данных']*9+[note]
            requests=[{'appendCells':{'sheetId':sheets['Головная боль'],'rows':[{'values':[cell(v,i==0) for i,v in enumerate(values)]}],'fields':'userEnteredValue,userEnteredFormat.numberFormat'}}]
            message='Начало боли записано.'
        else:
            open_rows=[r for r in data['headaches'] if not r.get('Прошла') and timedelta(0)<=now-datetime.fromisoformat(r['startedAt'])<=timedelta(days=2)]
            latest=max(open_rows,key=lambda r:r['row']) if open_rows else None
            # Discover actual header positions rather than assume a column order on writes.
            head=google('GET','/values/'+quote("'Головная боль'!1:1",safe=''),params={}).get('values',[[]])[0]
            def update(name,value):
                if name not in head: raise APIError('Не найден столбец «'+name+'».',502)
                requests.append({'updateCells':{'start':{'sheetId':sheets['Головная боль'],'rowIndex':latest['row']-1,'columnIndex':head.index(name)},'rows':[{'values':[cell(value)]}],'fields':'userEnteredValue'}})
            if action=='end':
                if not latest: raise APIError('Нет незавершённого приступа за последние двое суток.')
                duration=max(0,round((now-datetime.fromisoformat(latest['startedAt'])).total_seconds()/60))
                update('Прошла',now.strftime('%d.%m.%Y %H:%M')); update('Длилась',f'{duration//60} ч {duration%60} мин' if duration>=60 else f'{duration} мин')
                message='Окончание боли записано.'
            else:
                name=text_input(body,'name',150)
                if not name: raise APIError('Введите название таблетки.')
                if 'Таблетки' not in sheets: raise APIError('Сначала создайте лист «Таблетки» обычной командой Siri.')
                serial=(now.replace(tzinfo=None)-datetime(1899,12,30)).total_seconds()/86400
                values=[serial,now.strftime('%d.%m.%Y'),now.strftime('%H:%M'),name]
                requests.append({'appendCells':{'sheetId':sheets['Таблетки'],'rows':[{'values':[cell(v,i==0) for i,v in enumerate(values)]}],'fields':'userEnteredValue,userEnteredFormat.numberFormat'}})
                if latest:
                    old=str(latest.get('Таблетка','')); update('Таблетка',(old+'; ' if old else '')+now.strftime('%H:%M')+' — '+name)
                message='Приём таблетки записан.'
        # Reject incompatible layouts before adding a new headache.
        if action=='start':
            actual=google('GET','/values/'+quote("'Головная боль'!1:1",safe='')).get('values',[[]])[0]
            if actual[:len(HEADERS)]!=HEADERS: raise APIError('Структура столбцов отличается от исходного журнала. Запись остановлена, чтобы не сместить данные.',409)
        google('POST',':batchUpdate',json={'requests':requests})
        CACHE.update(time=0,data=None)
        return {'ok':True,'message':message}

class Handler(BaseHTTPRequestHandler):
    server_version='Journal'
    def log_message(self,*args): pass
    def response(self,status,data,cookie=None):
        content=json.dumps(data,ensure_ascii=False).encode()
        self.send_response(status); self.send_header('Content-Type','application/json; charset=utf-8'); self.send_header('Cache-Control','no-store'); self.send_header('X-Content-Type-Options','nosniff')
        if cookie: self.send_header('Set-Cookie',cookie)
        self.send_header('Content-Length',str(len(content))); self.end_headers(); self.wfile.write(content)
    def authenticated(self):
        try:
            cookies=SimpleCookie(self.headers.get('Cookie','')); token=cookies['golova_session'].value
            with LOCK: return SESSIONS.get(token,0)>time.time()
        except (KeyError,ValueError): return False
    def body(self):
        if self.headers.get('Content-Type','').split(';')[0]!='application/json': raise APIError('Требуется JSON.',415)
        size=int(self.headers.get('Content-Length','0'))
        if not 0<size<=16384: raise APIError('Некорректный размер запроса.',413)
        body=json.loads(self.rfile.read(size))
        if not isinstance(body,dict): raise APIError('Некорректный запрос.')
        return body
    def do_GET(self):
        try:
            path=urlparse(self.path).path
            if path=='/golova/api/status': return self.response(200,{'configured':KEY.exists(),'authenticated':self.authenticated()})
            if path!='/golova/api/data': raise APIError('Не найдено.',404)
            if not self.authenticated(): raise APIError('Войдите в дневник.',401)
            self.response(200,get_data())
        except APIError as e: self.response(e.status,{'error':e.message})
        except Exception: self.response(502,{'error':'Ошибка подключения к таблице. Повторите позже.'})
    def do_POST(self):
        try:
            if self.headers.get('Origin') not in ORIGINS: raise APIError('Недопустимый источник запроса.',403)
            path=urlparse(self.path).path; body=self.body()
            if path=='/golova/api/login':
                ip=self.headers.get('X-Real-IP',self.client_address[0]); now=time.time()
                with LOCK:
                    for key in list(FAILURES):
                        if now-FAILURES[key][1]>=900: del FAILURES[key]
                    count,last=FAILURES.get(ip,(0,now))
                    if count>=8: raise APIError('Слишком много попыток. Подождите 15 минут.',429)
                    cfg=config(); password=body.get('password','')
                    if not isinstance(password,str) or len(password)>512: raise APIError('Некорректный пароль.')
                    digest=hashlib.pbkdf2_hmac('sha256',password.encode(),bytes.fromhex(cfg['salt']),300000).hex()
                    if not hmac.compare_digest(digest,cfg['password_hash']):
                        FAILURES[ip]=(count+1,last); raise APIError('Неверный пароль.',401)
                    FAILURES.pop(ip,None)
                    for token in list(SESSIONS):
                        if SESSIONS[token]<=now: del SESSIONS[token]
                    token=secrets.token_urlsafe(32); SESSIONS[token]=now+28800
                return self.response(200,{'ok':True},'golova_session='+token+'; Path=/golova; Secure; HttpOnly; SameSite=Strict; Max-Age=28800')
            if not self.authenticated(): raise APIError('Войдите в дневник.',401)
            if path=='/golova/api/logout':
                cookies=SimpleCookie(self.headers.get('Cookie',''))
                with LOCK: SESSIONS.pop(cookies['golova_session'].value,None)
                return self.response(200,{'ok':True},'golova_session=; Path=/golova; Secure; HttpOnly; SameSite=Strict; Max-Age=0')
            if path=='/golova/api/record': return self.response(200,write_action(body))
            raise APIError('Не найдено.',404)
        except APIError as e: self.response(e.status,{'error':e.message})
        except (ValueError,json.JSONDecodeError): self.response(400,{'error':'Некорректный запрос.'})
        except Exception: self.response(502,{'error':'Не удалось подтвердить запись. Не повторяйте сразу: обновите дневник и проверьте таблицу.'})

if __name__=='__main__':
    ThreadingHTTPServer(('127.0.0.1',8177),Handler).serve_forever()
