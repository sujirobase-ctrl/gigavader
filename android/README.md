# BUIDL Android 2.0.0

App Android nativo (Java Views) com o núcleo Python incluído via Chaquopy. A
análise, as prévias e os downloads acontecem no aparelho. A captura de cookies
abre o navegador interno apenas para o login no site.

O APK gerado pelo GitHub Actions é debug e serve para instalação manual. O app
requer Android 7+ e processador arm64-v8a ou x86_64.

## Compilar no Windows

Requer JDK 21, Android SDK 36/build-tools 36.0.0 e Python 3.14:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File android\build.ps1
```

O wrapper Gradle usa a versão registrada em `gradle/wrapper/gradle-wrapper.properties`.
O Actions gera `BUIDL-2.0.0-Android.apk` e publica apenas esse APK e a GUI
Windows na release.

Use exclusivamente cookies próprios. Arquivos de cookies, estado local,
downloads e dados das simulações não devem ser adicionados ao repositório.
