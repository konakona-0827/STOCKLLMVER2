"""Explicit integration test: real quotes/OpenAI, isolated manual position fixtures, no orders.

Runs a visible Tk window for a MANUAL and a fixed-slot AUTO scan (120 seconds).
Closes after saving the verification JSON. Never writes production positions.
"""
import json
from contextlib import closing
from pathlib import Path
import sqlite3
import time
import tkinter as tk
import comtypes
from config import ROOT, now
from dashboard import Dashboard
from main import save_json
import position_store as ps
from position_analyzer import sell_qty


def main():
    directory = ROOT/'data'/'gui_verification'/now().strftime('%Y%m%dT%H%M%S')
    db = directory/'market_history.sqlite3'
    ps.save_manual(db,'0050',10,20,100,114.2)
    # 2454 was outside the last verified Top 10; adds POSITION-only coverage.
    ps.save_manual(db,'2454',0,2,None,1000)
    before = ps.load(db)
    root = tk.Tk()
    app = Dashboard(root,directory,120)
    report = dict(test_positions_only=True, data_directory=str(directory), real_order_sent=False)
    launched=time.monotonic()
    seen = set()
    def begin():
        app.start_auto()
        # Ensure a full 120 second slot in this integration test, to finish MANUAL first.
        app.scheduler.next_auto=time.time()+120
        target=app.scheduler.next_auto
        app.manual()
        report.update(auto_target=target,manual_preserved_schedule=app.scheduler.next_auto==target,
                      cooldown_after_manual=app.scheduler.cooldown(time.monotonic()),
                      manual_button_disabled=app.run_button.instate(['disabled']))
        app.manual()
        report['duplicate_manual_blocked']=app.scheduler.scan_lock.locked()
    def poll():
        if app.result:
            manifest=app.result['manifest']
            seen.add(manifest['trigger_type'])
            report[manifest['trigger_type']]=dict(manifest=manifest,
                top10=[r['symbol'] for r in app.result['top']],
                analysis_set=[{'symbol':c['symbol'],'source':c['analysis_source']} for c in app.result['analysis_set']],
                positions=app.positions, decisions=app.result['decision'],
                position_table=[app.position_tree.item(i)['values'] for i in app.position_tree.get_children()])
        if {'AUTO','MANUAL'} <= seen and not app.scheduler.scan_lock.locked():
            report.update(heartbeat_count=app.heartbeat,elapsed_seconds=time.monotonic()-launched,
                          positions_unchanged=ps.load(db)==before, sell_0050_half_qty=sell_qty(20,.5),
                          cooldown_after_60_seconds=app.scheduler.cooldown(time.monotonic()))
            with closing(sqlite3.connect(db)) as connection:
                report['sqlite_integrity']=connection.execute('PRAGMA integrity_check').fetchone()[0]
                report['run_count']=connection.execute('SELECT count(*) FROM scan_runs').fetchone()[0]
            save_json(directory/'verification.json',report)
            print(json.dumps(dict(report_path=str(directory/'verification.json'),heartbeat=app.heartbeat,
                                 validations={k:report[k]['manifest']['ai_validation'] for k in ('MANUAL','AUTO')})),flush=True)
            app.close()
            return
        if time.monotonic()-launched>290:
            report['error']='GUI_INTEGRATION_TIMEOUT: '+app.last_error
            save_json(directory/'verification.json',report)
            print(json.dumps(dict(report_path=str(directory/'verification.json'),error=report['error'])),flush=True)
            app.close()
            return
        root.after(200,poll)
    root.after(500,begin)
    root.after(700,poll)
    root.mainloop()


if __name__=='__main__':
    main()
