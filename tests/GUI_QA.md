# Simulações e testes da GUI Windows

Use apenas mídias artificiais do servidor local. Mantenha o campo de cookies
vazio e uma pasta de downloads exclusiva para cada execução.

## Simulação da galeria

Execute `python -X utf8 tests/gui_preview_smoke.py`.
O teste abre janelas Tk reais e verifica paginação, filtros, seleção,
reprodução/pausa, cache, cancelamento e geometria do layout. As imagens,
os frames e o downloader desse teste são simulados.

## Fluxo completo com downloads locais

Em um terminal, execute `python -u android/qa_server.py`.
O servidor atende em `http://127.0.0.1:8876/threads/qa` e oferece duas páginas,
três fotos, um vídeo, um ZIP e uma resposta de vídeo indisponível.

Em outro terminal, execute, a partir da raiz do código corrigido:

```powershell
python -X utf8 tests/gui_local_e2e.py `
  --executable "dist\buidl-gui.exe" `
  --evidence "build\GUI-QA-nova-execucao" `
  --downloads "build\gui-downloads-nova-execucao"
```

Use uma pasta de downloads vazia. Omitir `--executable` testa os módulos do
código-fonte. Com essa opção, o teste carrega o código compilado de `gui` e
`src` de dentro do EXE e o executa no Python de testes, usando uma janela Tk
real. Os widgets e callbacks da interface são acionados programaticamente.
O downloader, o cliente HTTP e a decodificação do vídeo são os reais.

O teste verifica abertura, análise das duas páginas, filtros, busca, prévia de
vídeo, pausa/retomada da prévia, tratamento da prévia indisponível, seleção,
download, hashes dos arquivos, reabertura da galeria, arquivos já existentes
e cancelamento. Salva capturas e `resultado.json` em `--evidence`.

Uma prévia de vídeo indisponível deve manter a galeria e não contar como falha
de download. As mídias não selecionadas não devem ser salvas.

Os resultados documentam o comportamento com o servidor local; a conectividade
e as respostas dos hosts externos exigem validação separada.
