# Jev bounded computer loop

This runnable client keeps the dependency direction explicit:

```
Jev-powered external harness → Mola action session → Omarchy computer
```

The harness asks Jev to choose one value from a fresh, typed action set. Ordinary
code validates that choice and executes the corresponding fixed action through
Mola. Model output never becomes a shell command, selector, coordinate, or URL.
The loop is capped by `JEV_MAX_STEPS` and refuses an early `done`.

Start a tiny fixture server in a ready Mola computer, then run:

```sh
export MOLA_API=http://127.0.0.1:4141/v1
export MOLA_TOKEN=...
export MOLA_MACHINE=...
export TYPESAFE_API_KEY=...       # optional; absent runs the offline policy
node examples/jev/fast-computer-loop/index.mjs
```

The offline policy exercises the same validation and Mola side effects without
a paid credential. Jev is optional and remains outside Mola's dependencies.
