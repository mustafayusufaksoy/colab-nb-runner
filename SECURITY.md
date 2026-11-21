# Security

## Reporting a vulnerability

Please **do not open a public issue**. Send a [private security advisory](https://github.com/mustafayusufaksoy/colab-nb-runner/security/advisories/new) or a DM to [@codewandertech](https://x.com/codewandertech). I'll reply within a few days.

Examples: the runner executing code it shouldn't (e.g. through `--patch` or `--skip` handling), leaking the contents of a `--config` session file, or writing secrets into logs.

## Things to keep in mind

- The runner sends your notebook's code cells to the Colab session you point it at, exactly as `colab exec` would. Only run notebooks you trust.
- `--config` points at the Colab CLI's session state file. Treat it like a credential; don't commit it or attach it to issues.
- Issues in the Colab CLI itself belong to that project.

---

## Türkçe

Güvenlik sorunlarını herkese açık issue yerine [özel güvenlik bildirimi](https://github.com/mustafayusufaksoy/colab-nb-runner/security/advisories/new) ya da X'ten [@codewandertech](https://x.com/codewandertech)'e özel mesajla bildir. `--config` ile verilen Colab oturum dosyası bir kimlik bilgisi gibidir; paylaşma.
