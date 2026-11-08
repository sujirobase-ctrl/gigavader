"""Exercise actual WebView2 HTML, JS bridge and local HTTP downloads."""
import hashlib
import json
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

archive = None
if len(sys.argv) > 1:
    import importlib.abc
    import importlib.util
    from PyInstaller.archive.readers import CArchiveReader
    archive = CArchiveReader(sys.argv[1])
    pyz = archive.open_embedded_archive('PYZ.pyz')
    class PackagedModules(importlib.abc.MetaPathFinder, importlib.abc.Loader):
        def find_spec(self, fullname, path=None, target=None):
            if (fullname == 'src' or fullname.startswith('src.')) and fullname in pyz.toc:
                return importlib.util.spec_from_loader(fullname, self, is_package=pyz.toc[fullname][0] == 1)
        def create_module(self, spec): return None
        def exec_module(self, module):
            module.__file__ = str(ROOT / (module.__name__.replace('.', '/')+'.py'))
            exec(pyz.extract(module.__name__), module.__dict__)
    sys.meta_path.insert(0, PackagedModules())

import webview
from src.desktop import DesktopAPI, DesktopController
from android.qa_server import Handler, DATA
from http.server import ThreadingHTTPServer


def main():
    evidence = ROOT.parent/'Documentacao'
    workspace = Path(tempfile.mkdtemp(prefix='buidl-desktop-qa-'))
    controller = DesktopController(workspace/'state')
    server = ThreadingHTTPServer(('127.0.0.1', 8876), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    def asset(name):
        if archive:
            key = next(k for k in archive.toc if k.replace('\\', '/') == 'desktop_ui/'+name)
            return archive.extract(key).decode('utf-8')
        return (ROOT/'desktop_ui'/name).read_text(encoding='utf-8')
    html = asset('template.html')+'<script>'+asset('app.js')+'</script></body></html>'
    window = webview.create_window('BUIDL 1.4.0 — Teste GUI', html=html, js_api=DesktopAPI(controller), width=1180, height=820)
    controller.window = window
    checks, errors = [], []

    def test():
        def js(code): return window.evaluate_js(code)
        def wait(expression, timeout=25):
            deadline = time.monotonic()+timeout
            while time.monotonic()<deadline:
                if js(expression): return
                time.sleep(.15)
            raise AssertionError(expression + '\n' + str(list(controller.logs)))
        def check(label, condition):
            assert condition, label
            checks.append(label); print('PASS',label,flush=True)
        try:
            wait("typeof api !== 'undefined' && !!api")
            check('WebView2 e ponte Python carregados',True)
            js("""showCookieStatus({captured:true, state:'valid', checked_at:new Date().toISOString(),
                expires:{login:new Date(Date.now()+3600000).toISOString(),protection:null}})""")
            check('Cookies mostram prazo e verificação separados', js(
                "$('#ckst').textContent.includes('Cookie de login expira em') && "
                "$('#ckst').textContent.includes('O site confirmou o login') && !!$('#ck-login')"))
            js('fill('+json.dumps(dict(url='http://127.0.0.1:8876/threads/qa',destination=str(workspace/'downloads'),delay=0,connections=1,retries=0))+')')
            js("$('#vz').click()")
            wait("S.state==='gallery' && S.items.length===6 && !$('#v-pick').hidden")
            check('Visualizar captura duas páginas pelo motor real',len(controller.prepared.plan)==6)
            check('Captura não baixa mídia',not list((workspace/'downloads').rglob('*.jpg')))
            wait("document.querySelectorAll('#gal img').length > 0")
            check('Miniaturas reais carregadas',True)
            wait("S.items.some(f=>f.name==='video.mp4' && S.thumbs.get(f.id)?.frames.length===1)")
            check('Vídeo sem poster recebe capa antes do clique',js('S.video===null'))
            js("document.querySelector('#gal [data-image]').click()")
            wait("!$('#full-image').hidden && $('#full-image').naturalWidth>0")
            check('Imagem ampliada dentro do aplicativo', js("!$('#image-modal').hidden && getComputedStyle($('#full-image')).objectFit==='contain'"))
            check('Galeria sem botão de navegador externo', js("document.querySelector('#gal [data-origin]')===null"))
            js("$('#image-close').click()")
            check('Fechar ampliação retorna à galeria',js("$('#image-modal').hidden && !$('#v-pick').hidden"))

            js("document.querySelector('#gal [data-video]').click()")
            wait("S.frames.length>1")
            check('Prévia real de vídeo decodificada',True)
            check('Vídeo ampliado com proporção preservada', js("getComputedStyle($('#video-frame')).objectFit==='contain' && $('#md .pl').getBoundingClientRect().height>300"))
            wait("S.frame>0")
            check('Ver prévia inicia reprodução automaticamente',js("$('#pr').textContent==='Pausar'"))
            js("$('#pr').click()")
            check('Reproduzir e pausar prévia',js("$('#pr').textContent==='Reproduzir'"))
            js("$('#mc').click(); $('#sq').value='photo'; $('#sq').dispatchEvent(new Event('input'))")
            check('Fechar vídeo libera os quadros da interface',js('S.frames.length===0'))
            check('Busca filtra a galeria',js("document.querySelectorAll('#gal [data-id]').length===3"))
            js("S.sel.clear(); $('#sv').click(); $('#enq').click()")
            wait("S.state==='done'")
            check('Baixar selecionados grava três imagens',len(list((workspace/'downloads').rglob('*.jpg')))==3)
            check('Fila esvazia após concluir a seleção',js("!$('#qb [data-row]')"))
            for name in ('photo1.jpg','photo2.jpg','photo3.jpg'):
                check('Hash '+name,hashlib.sha256(next((workspace/'downloads').rglob(name)).read_bytes()).digest()==hashlib.sha256(DATA['/'+name][0]).digest())
            js("S.pick=false;tab('novo');$('#an').click()")
            wait("S.state==='partial'")
            check('Baixar tudo inicia automaticamente e apresenta falha real',any(r['status']=='fail' for r in controller.rows.values()))
            js("$('#bf').click()")
            check('Janela de falhas contém diagnóstico real',js("!$('#fm').hidden && $('#fl').textContent.includes('unavailable')"))
            check('Falhas permitem conferir a origem no navegador',js("!!$('#fl button[data-check]')"))
            js("$('#fa').click()")
            wait("S.state==='downloading'")
            wait("S.state==='partial'")
            check('Repetição de falhas pelo botão',len(controller.history)>=3)
            js("$('#fx').click();tab('hist')")
            check('Histórico real exibido',js("$('#hb').textContent.includes('Parcial')"))
            js("tab('diag');$('#timing-refresh').click()")
            wait("!!$('#timing-list tbody tr')")
            check('Diagnóstico mostra conclusão, duração e vazão',js(
                "$('#timing-list').textContent.includes('photo1.jpg') && $('#timing-list').textContent.includes('Vazão')"))
            # A large prepared queue uses the same controller and real DOM, with synthetic metadata only.
            from src.downloaders import PreparedThread
            from src.resolvers import ResolvedMedia
            root = workspace/'large'
            controller._prepared(PreparedThread('Fila de 12800 arquivos',root,[(ResolvedMedia(f'https://example.invalid/{i}.zip',f'{i}.zip'),root) for i in range(12800)],20))
            with controller.lock:
                controller.requested_ids = set(controller.rows)
                controller.rows[100]['status'] = 'down'
            wait('S.items.length===12800')
            js("tab('down')")
            wait("$('#qb [data-row]')?.dataset.row==='100'")
            check('Fila grande mostra até 50 itens e prioriza os ativos',js("document.querySelectorAll('#qb .it').length===50 && !$('#qa-next')"))
            with controller.lock:
                controller.rows[100]['status'] = 'ok'
                controller.rows[0]['status'] = 'skip'
                controller.rows[1]['status'] = 'fail'
                controller.dirty.update([100, 0, 1])
            wait("$('#qb [data-row]')?.dataset.row==='2'")
            check('Arquivos finalizados saem e dão lugar aos próximos',js("document.querySelectorAll('#qb .it').length===50 && !!$('#qb [data-row=\"51\"]')"))
            check('Histórico persiste após reiniciar controlador',len(DesktopController(workspace/'state').history)>=3)
        except Exception:
            errors.append(traceback.format_exc());print(errors[-1],flush=True)
        finally:
            evidence.mkdir(exist_ok=True)
            (evidence/('Teste-GUI-Empacotado.json' if archive else 'Teste-GUI.json')).write_text(json.dumps({'checks':checks,'errors':errors,'workspace':str(workspace)},ensure_ascii=False,indent=2),encoding='utf-8')
            controller.cancel_download()
            window.destroy()
    webview.start(test,gui='edgechromium',private_mode=True)
    server.shutdown()
    return bool(errors)


if __name__ == '__main__':
    raise SystemExit(main())
