# 🛰️ Mission Control — Deployment Runbook (Part 3)
## 3D Dashboard + Janet + Facts + Alerts — όλα σε ένα, στο Pi

> **Προϋπόθεση:** Part 1 (Pi online μέσω Tailscale). Part 2 (Redis memory) προαιρετικό αλλά χωρίς αυτό το Facts panel θα είναι άδειο.
> **Χρόνος:** ~15 λεπτά.

---

## Τι είναι

Ένα web app που τρέχει στο Pi (`http://100.107.28.116:8080`) και σου δίνει:

- **3D Janet core** — ζωντανό wireframe icosphere με orbit rings, glow, starfield. Αντιδρά: idle (αργό pulse) → thinking (γρήγορο spin) → speaking. Όχι 2D blob.
- **Janet chat** — NVIDIA NIM default (auto-routing 8B/70B/72B κατά μήκος μηνύματος), με dropdown για manual επιλογή και **CLAUDE ⚡ mode** για τα σημαντικά (με budget cap $2/μέρα — πάνω από αυτό, αυτόματο fallback σε NVIDIA δωρεάν).
- **Shared memory panel** — τα facts από το Redis (Part 2), live. `/remember <κάτι>` στο chat αποθηκεύει fact.
- **Alerts & tips** — αυτόματα: bot down, Pi ζεσταίνεται, δίσκος γεμίζει, Redis offline, budget 80%+. Συν custom notifications που μπορεί να push-άρει οποιοσδήποτε agent στο Redis list `notifications`.
- **System status** — CPU temp, load, disk, uptime, services (telegram-agent, tailscaled), keys status.
- **Mobile-ready** — responsive, δουλεύει σαν PWA στο iPhone (Add to Home Screen), προσβάσιμο από παντού μέσω Tailscale app.

---

## Deploy (στο Pi)

### 1. Copy τον φάκελο

Από το Mac:
```bash
scp -r ~/Downloads/mission-control alex@100.107.28.116:~/
```

### 2. Setup
```bash
ssh alex@100.107.28.116
cd ~/mission-control
bash setup.sh
```

### 3. Βάλε τα keys
```bash
nano .env
# NVIDIA_API_KEY=nvapi-7aJQ...        ← το υπάρχον key σου
# UPSTASH_REDIS_REST_URL=rediss://...  ← από Upstash console (native, όχι REST)
# UPSTASH_REDIS_REST_TOKEN=...
# ANTHROPIC_API_KEY=sk-ant-...         ← ΠΡΟΑΙΡΕΤΙΚΟ, μόνο αν θες Claude escalation
```

### 4. Start
```bash
sudo systemctl start mission-control
journalctl -u mission-control -f   # δες ότι ξεκίνησε καθαρά
```

### 5. Άνοιξε
- Από οποιαδήποτε συσκευή με Tailscale: **http://100.107.28.116:8080**
- iPhone: Safari → Share → **Add to Home Screen** → τώρα έχεις app icon

---

## Πώς δουλεύει το routing (NVIDIA πρώτα, Claude για τα σημαντικά)

| Mode | Model | Κόστος | Πότε |
|---|---|---|---|
| AUTO | Llama 8B / Nemotron 70B / Qwen 72B κατά μήκος | Δωρεάν | Default — καθημερινή χρήση |
| FAST | Llama 8B | Δωρεάν | Γρήγορες ερωτήσεις |
| MAIN | Nemotron 70B | Δωρεάν | Κύρια δουλειά |
| DEEP | Qwen 72B | Δωρεάν | Ανάλυση, μεγάλο context |
| CLAUDE ⚡ | Sonnet | ~$0.01-0.02/μήνυμα | Σημαντικές αποφάσεις, ποιότητα |

**Budget guard:** Το CLAUDE mode μετράει spend στο Redis. Πάνω από 80% του `DAILY_BUDGET_USD` βγαίνει alert· στο 100% κάνει σιωπηλό fallback σε Qwen (δωρεάν). Δεν θα ξαναγίνει το June 7 incident.

**Codex:** Δεν έχει API με το plan σου — τρέχει ως CLI στο Mac. Η συνεργασία μένει όπως την έχεις: Claude Code κάνει orchestrate, Codex CLI παίρνει mechanical tasks. Το dashboard δεν τον καλεί απευθείας (αν κάποια στιγμή θες remote Codex trigger από το dashboard, γίνεται με ένα μικρό webhook στο Mac — πες μου).

---

## Janet: τι την κάνει «πιο έξυπνη» τώρα

1. **Shared memory στο context** — κάθε μήνυμα προς Janet φορτώνει τα 10 πιο πρόσφατα facts από το Redis. Ό,τι μαθαίνει ο Telegram bot ή ο Claude, το ξέρει και η Janet.
2. **`/remember`** — γράφει από το chat κατευθείαν στη shared memory → nightly sync → vault → GitHub.
3. **Nemotron 70B personality** — sharp/warm system prompt, ξέρει τα projects σου, μιλάει τη γλώσσα σου.
4. **Escalation** — όταν το θέμα είναι σοβαρό, γυρνάς το dropdown σε CLAUDE ⚡.

**Τι ΔΕΝ έχει (ειλικρινά):** Το voice (STT/TTS) της παλιάς Janet στο Mac δεν μεταφέρθηκε εδώ — το faster-whisper θέλει CPU που το Pi δεν έχει άνετα, και το macOS `say` δεν υπάρχει σε Linux. Options αν θες voice στο Pi: browser Web Speech API (δωρεάν, τρέχει στο κινητό σου, όχι στο Pi) — μπορώ να το προσθέσω στο UI σε επόμενο βήμα. Η Mac Janet (`~/janet/`) συνεχίζει να δουλεύει ανεξάρτητα για voice.

---

## Αρχιτεκτονική — πλήρης εικόνα πλέον

```
                        ┌─ MISSION CONTROL (Pi :8080) ─┐
  iPhone/Mac/laptop ───→│  3D UI · Janet chat · Facts   │
   (μέσω Tailscale)     │  Alerts · Status              │
                        └───────┬──────────┬────────────┘
                                │          │
                     NVIDIA NIM (free)   Upstash Redis ←── Telegram bot γράφει
                     Claude API (budget)      │
                                              ↓ nightly sync (Mac)
                                        Vault → git push → GitHub (private)
```

---

## ✅ Checklist

```
[ ] mission-control/ folder copied στο Pi
[ ] bash setup.sh — no errors
[ ] .env: NVIDIA key + Upstash creds (+ Anthropic optional)
[ ] sudo systemctl start mission-control
[ ] http://100.107.28.116:8080 ανοίγει, 3D core κινείται
[ ] Chat test: "γεια" → Janet απαντά (tag: nvidia · llama-3.1-8b)
[ ] /remember test → fact εμφανίζεται στο Facts panel
[ ] Status panel: TG BOT = active (αν έκανες το Part 1)
[ ] iPhone: Add to Home Screen
```

## Troubleshooting

| Πρόβλημα | Λύση |
|---|---|
| Σελίδα δεν φορτώνει | `journalctl -u mission-control -n 50` — δες το error |
| Janet: "Backend δεν απαντά" | Το uvicorn έπεσε — `systemctl restart mission-control` |
| Facts panel άδειο | Redis creds λάθος στο .env, ή δεν έχεις γράψει κανένα fact ακόμα |
| CLAUDE mode απαντά με Qwen | Είτε δεν έβαλες ANTHROPIC_API_KEY, είτε χτύπησες το budget cap |
| 3D δεν φαίνεται (μαύρο) | Παλιός browser χωρίς WebGL — δοκίμασε Safari/Chrome update |
| Αργό στο κινητό | Φυσιολογικό σε πολύ παλιές συσκευές· το starfield είναι το βαρύ κομμάτι |

---

## Μετά (όταν το θες)

- **Cloudflare Tunnel** → πραγματικό online (https://control.δικόσουdomain) χωρίς Tailscale app. ~20 λεπτά setup.
- **Voice στο UI** — Web Speech API push-to-talk κουμπί (δωρεάν, browser-side).
- **Codex webhook** — κουμπί στο dashboard που στέλνει task στο Mac για Codex εκτέλεση.
