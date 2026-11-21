# Contributing

Thanks for helping. Useful contributions, most valuable first:

1. **Bug reports from real runs.** The runner exists because the Colab CLI misbehaves in ways that are hard to predict (exit code 0 after an exception, hung connections, duplicated cells). If a run failed, hung or ran a cell twice, open a [bug report](https://github.com/mustafayusufaksoy/colab-nb-runner/issues/new?template=bug.yml) with the runner's output and your Colab CLI version.
2. **Fixes.** Fork, branch, and keep the change focused.
   - `python -m unittest discover -s tests -v` must pass (CI runs it on Python 3.9 and 3.13, Linux and macOS).
   - No new runtime dependencies: the package is standard-library only on purpose.
   - If you change behaviour, update the option table and "Things to know" in the README.
3. **Measurements.** If you claim a fix makes runs more reliable, describe how you measured it (notebook, GPU type, how many runs).

Never paste Colab session state files, OAuth tokens or notebook outputs that contain private data into issues.

Security issues: see [SECURITY.md](SECURITY.md).

---

## Türkçe

Katkı için teşekkürler. En faydalısı gerçek çalıştırmalardan **hata bildirimi** (runner çıktısı ve Colab CLI sürümüyle). Değişiklik yaparsan testler geçmeli, yeni bağımlılık eklenmemeli ve davranış değiştiyse README güncellenmeli. Colab oturum dosyası, token ya da özel veri içeren çıktı paylaşma. Güvenlik sorunları için [SECURITY.md](SECURITY.md).
