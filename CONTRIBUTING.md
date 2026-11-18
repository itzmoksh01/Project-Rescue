# Contributing

1. Fork, branch, change.
2. Run the tests (no network, no real home folder is touched):
   ```bash
   python3 -m unittest discover -s tests -v
   ```
3. Keep it **single-file and zero-dependency** (`project_rescue.py`, standard library, Python 3.8+).
4. New behavior needs an end-to-end test in `tests/test_project_rescue.py` using the existing fake-`HOME` helpers.
5. Safety rules are not negotiable: never overwrite user files, never push to public repos, never let a hook exit non-zero or block.

Good first issues: Windows CI hardening, Cursor/Windsurf installers, `project-rescue doctor`.
