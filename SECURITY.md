# Security

## What Project Rescue does with your data
- Copies project files to a local folder you control.
- Pushes to a **private** GitHub repo **you** own, using your existing `git` / `gh` credentials. It never stores tokens.
- Excludes secret-looking files from GitHub: `.env*` (except `.env.example|sample|template|dist`), `*.pem`, `*.key`, `*.p12`, `*.pfx`, `*.jks`, `*.keystore`, SSH keys, `credentials.json`, `client_secret*.json`, `service-account*.json`, `secrets.{json,yml,yaml,toml}`, `.npmrc`, `.pypirc`, `.netrc`, `.htpasswd`, `*.tfstate`, anything under `.ssh/`, `.aws/credentials`.
- Refuses to push when `gh` reports the repo is public.

## Things to know
- The local mirror contains **everything**, secrets included. Keep it on a disk only you can read (disk encryption recommended).
- Secret detection is name-based. A secret hard-coded in an ordinary source file **will** be pushed to your private repo. Do not hard-code secrets.
- Your project's git history (as a bundle) is pushed. If a secret was ever committed, it is in that history, same as in your own repo.
- Hooks run commands with your user's privileges. Project Rescue installs exactly four hook entries, all calling `agent-hook` on the installed engine. Inspect them in `~/.claude/settings.json`.
- Without `gh`, repository visibility cannot be verified; `init` warns about this.

## Reporting a vulnerability
Open a private security advisory on the repository (Security tab, "Report a vulnerability"). Please do not file public issues for security problems.
