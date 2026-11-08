# Testes de filas longas e recuperação

Execute `python -m pytest tests/test_long_sessions.py -q --durations=3`.

Os cenários cobrem uma thread de 20 páginas com 12.800 arquivos, um álbum
Bunkr do mesmo tamanho, fila de 12.800 trabalhos com no máximo seis tarefas
pendentes para três workers, renovação após avanço simulado de 55 horas,
atualização do cookies.txt, HTTP 401/403/404/410, orçamento de tentativas de 429,
continuidade da fila após recusas de acesso repetidas, paginação GoFile,
reutilização de conta GoFile, gravação por arquivo e último registro truncado.
O avanço de 55 horas é simulado; não representa uma execução física por 55 horas.

## Encerramento forçado e reinício da GUI

```powershell
python -X utf8 tests/gui_crash_recovery.py `
  --executable "dist\buidl-gui.exe" `
  --evidence "build\qa-crash-nova-execucao"
```

Esse teste abre uma GUI com um servidor local e mídias artificiais, aguarda
arquivos concluídos e partes incompletas, mata o processo e inicia o EXE real.
Clique em **Sim** na pergunta de retomada. O teste confere os 20 hashes, a
retomada por Range e a ausência de novos downloads dos arquivos concluídos.

Com `--automatic`, a segunda etapa executa o código compilado de GUI/src extraído
do EXE em uma janela Tk de testes e responde Sim automaticamente. A pergunta
é registrada em `runtime/recovery-question.json`. Essa modalidade é útil em
ambientes nos quais o controle de janelas nativas não consegue acionar o diálogo.

Os diretórios de evidência devem ser novos. `BUIDL_RECOVERY_DIR` isola os dados
das simulações dos dados pessoais do aplicativo. Não use cookies pessoais.
A simulação de encerramento abrupto não corta a energia física do computador.
