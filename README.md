# gigavader

BUIDL é um aplicativo para organizar e baixar mídias de tópicos XenForo. A versão para Windows usa Microsoft Edge WebView2.

## Windows

Baixe o executável na [prévia v2.0.0](https://github.com/sujirobase-ctrl/gigavader/releases/tag/v2.0.0). Essa versão inclui retomada de filas, recuperação do Bunkr com uma conexão após erros temporários, reaproveitamento de espelhos e diagnóstico por arquivo. A opção **Entrar no SimpCity** guarda a sessão com proteção do Windows e exibe a expiração informada pelo cookie, quando disponível.

Para compilar a interface Windows com Python 3.14 e WebView2:

```powershell
python -m pip install -r requirements.txt
python -m PyInstaller --clean --noconfirm buidl-desktop.spec
```

O executável é gerado em `dist/BUIDL-2.0.0-Teste.exe`. As instruções do código Android estão em [android/README.md](android/README.md).

## Origem do código

Esta versão parte do [Simpsus](https://github.com/sujirobase-ctrl/Simpsus). Os resolvers e a detecção de hosts usam o [ForumPostDownloader](https://github.com/SkyCloudDev/ForumPostDownloader) como referência. O limitador de velocidade deriva do [BunkrDownloader de Lysagxra](https://github.com/Lysagxra/BunkrDownloader) e foi adaptado em novembro de 2026. A distribuição combinada inclui a [GNU GPL v3](LICENSE). O código próprio das versões anteriores tinha a licença MIT indicada nos metadados originais.
