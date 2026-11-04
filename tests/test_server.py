import importlib.util, unittest
from pathlib import Path
from unittest.mock import patch
from datetime import datetime,timedelta
from zoneinfo import ZoneInfo
spec=importlib.util.spec_from_file_location('journal',str(Path(__file__).resolve().parents[1]/'server.py'));j=importlib.util.module_from_spec(spec);spec.loader.exec_module(j)
class JournalTests(unittest.TestCase):
 def setUp(self):
  self.tz=ZoneInfo('Europe/Moscow'); self.now=datetime.now(self.tz)
  j.CACHE['sheets']={'Головная боль':7,'Таблетки':8}
 def test_dates_duration_and_missing(self):
  rows=[j.HEADERS,['04.11.2026 12:00:00','04.11.2026','12:00','Головная боль','Сильная','Висок','04.11.2026 13:30'],['','','']]
  r=j.convert(rows,self.tz)
  self.assertEqual(len(r),1);self.assertEqual(r[0]['durationMinutes'],90);self.assertEqual(r[0]['row'],2);self.assertEqual(r[0]['day'],'2026-11-04')
 def test_serial_roundtrip_and_literals(self):
  d=datetime(2026,11,4,12,30,tzinfo=self.tz)
  serial=(d.replace(tzinfo=None)-datetime(1899,12,30)).total_seconds()/86400
  self.assertLess(abs((j.parse_date(serial,self.tz)-d).total_seconds()),.01)
  self.assertEqual(j.cell('=IMPORTXML("evil")')['userEnteredValue'],{'stringValue':'=IMPORTXML("evil")'})
 def run_write(self,body,heads=None):
  row={'row':5,'startedAt':(self.now-timedelta(hours=1)).isoformat(),'Прошла':'','Таблетка':'12:00 — A'}
  dataset={'timezone':'Europe/Moscow','headaches':[row]}
  calls=[]
  def google(method,suffix='',**kwargs):
   calls.append((method,suffix,kwargs))
   return {'values':[heads or j.HEADERS]} if method=='GET' else {}
  with patch.object(j,'get_data',return_value=dataset),patch.object(j,'google',side_effect=google): result=j.write_action(body)
  return result,calls
 def test_start_creates_typed_timestamp(self):
  _,calls=self.run_write({'action':'start','severity':'Сильная','note':'=SUM(1,1)'})
  req=calls[-1][2]['json']['requests'][0]['appendCells'];cells=req['rows'][0]['values']
  self.assertEqual(len(cells),19);self.assertIn('numberValue',cells[0]['userEnteredValue']);self.assertEqual(cells[18]['userEnteredValue']['stringValue'],'=SUM(1,1)')
 def test_end_targets_existing_row_and_headers(self):
  heads=j.HEADERS[:];heads[6],heads[7]=heads[7],heads[6]
  _,calls=self.run_write({'action':'end'},heads)
  updates=calls[-1][2]['json']['requests']
  self.assertEqual(updates[0]['updateCells']['start'],{'sheetId':7,'rowIndex':4,'columnIndex':7})
 def test_pill_appends_and_links(self):
  _,calls=self.run_write({'action':'pill','name':'Ибупрофен'})
  req=calls[-1][2]['json']['requests'];self.assertEqual(len(req),2)
  self.assertEqual(req[0]['appendCells']['sheetId'],8)
  self.assertIn('Ибупрофен',req[1]['updateCells']['rows'][0]['values'][0]['userEnteredValue']['stringValue'])
 def test_wrong_schema_refuses_start(self):
  with self.assertRaises(j.APIError):self.run_write({'action':'start'},['Different'])
if __name__=='__main__': unittest.main()
