"""Exercise both GUI entry points against a real protection/login response.

Loads compiled GUI/src code from the selected EXE; uses real Tk, HTTP and cookies.
Intended for an inaccessible thread: it verifies the error before any media queue.
"""

import argparse
import json
import os
import tempfile
import time
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def run(args):
    from tests.gui_local_e2e import load_executable
    gui = load_executable(args.executable)
    from tests.gui_preview_smoke import pump, snapshot
    args.evidence.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='buidl-access-recovery-') as recovery:
        os.environ['BUIDL_RECOVERY_DIR'] = recovery
        dialogs = []
        gui.messagebox.showerror = lambda title, message, **kwargs: dialogs.append((title, message))
        app = gui.BuidlApp()
        result = {'mode': 'compiled GUI/src from EXE; real Tk and real authenticated HTTPS',
                  'executable': str(args.executable), 'cases': []}
        try:
            pump(app, .5)
            result['initial_output'] = app.output_entry.get()
            for entry, value in [(app.url_entry, args.url), (app.cookies_entry, str(args.cookies)),
                                 (app.output_entry, str(args.output))]:
                app._replace_entry(entry, value)
            for name, button in [('baixar-tudo', app.start_button), ('visualizar', app.preview_button)]:
                button.invoke()
                deadline = time.monotonic() + 65
                while app.running and time.monotonic() < deadline:
                    pump(app, .08)
                assert not app.running, 'Request did not finish within test deadline'
                log = app.log_box.get('1.0', 'end')
                assert 'FileNotFoundError' not in log, log
                assert 'HTTP 403' in log, log
                assert 'proteção anti-bot' in log, log
                assert app.total_files == 0 and app.last_counts['completed'] == 0
                assert app.progress_text.cget('text') == 'Não iniciado'
                snapshot(app, args.evidence / (name+'.png'))
                (args.evidence / (name+'.log')).write_text(log, encoding='utf-8')
                result['cases'].append({'action': name, 'status': 'PASS', 'http': 403,
                                        'tls_file_error': False, 'media_downloaded': 0})
                print('GUI REAL ACCESS PASS:', name, 'HTTPS succeeded; site returned protection 403', flush=True)
            result['dialogs'] = dialogs
            (args.evidence/'resultado.json').write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding='utf-8')
        finally:
            app.cancel_event.set()
            app.pause_event.set()
            if app.preview_window is not None:
                app.preview_window.close()
            app.destroy()


if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--executable', type=Path, required=True)
    parser.add_argument('--url', required=True)
    parser.add_argument('--cookies', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--evidence', type=Path, required=True)
    run(parser.parse_args())
