# Opening the Job Applier

The app starts by itself and should open in a new browser tab. If it did not:

1. At the bottom of this window, click the **Ports** tab.
2. On the row for port **8000** ("Job Applier"), hover the address and click the **globe icon** (Open in Browser).

If port 8000 is not listed, or the page says **HTTP ERROR 502**, the app is not running. Open a terminal
(menu ☰ → Terminal → New Terminal) and run:

```bash
bash .devcontainer/run.sh --foreground
```

Leave that terminal open: the app runs there and shows any errors. Then reload the app tab. Then follow `docs/HOSTING_GUIDE.md`, Part B.
