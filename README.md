# Sigmionary

A multiplayer picture-guessing game for Discord — think Pictionary, but with real photos.

Images are revealed one at a time as clues. The faster you guess, the more points you earn.

![Sigmionary Banner](sigmionary-banner.png)

---

## How to Play

### Starting a game

An admin runs:
```
/sigmionary start
```
or, for a specific number of rounds:
```
/sigmionary start rounds:5
```

The bot shuffles all available questions and starts the first round automatically.

---

### Each round

1. The bot announces the **category** and **sub-category** (e.g. *Kerala → Beach*).
2. Images are revealed **one at a time**, each acting as a progressively clearer clue.
3. **Type your answer directly in chat** — no slash command needed.
4. The bot uses fuzzy matching, so minor typos and spelling variations are accepted.
   - `Kaappil` → accepted as `Kappil`
   - `Mattancherri` → accepted as `Mattancherry`
5. The **first correct answer** wins the round. The question ends and the next one begins.
6. If nobody guesses after all images are shown, the answer is revealed and the game moves on.

---

### Scoring

| When you answer | Base points |
|---|---|
| After hint 1 (first image) | **100 pts** |
| After hint 2 | **70 pts** |
| After hint 3 | **40 pts** |

**Speed bonus** — answer quickly after a hint appears and earn up to **+30 extra points**. The bonus decays linearly over the 20-second hint window.

**Streak multipliers** — consecutive correct answers multiply your score:

| Streak | Multiplier |
|---|---|
| 2 in a row | ×1.2 |
| 3 in a row | ×1.5 |
| 5 in a row | ×2.0 |

Missing a round resets your streak to zero.

---

### Commands

| Command | Who | What it does |
|---|---|---|
| `/sigmionary start [rounds]` | Anyone | Start a game (optional round count) |
| `/sigmionary stop` | Mod | End the current game |
| `/sigmionary skip` | Mod | Skip the current question |
| `/sigmionary score` | Anyone | Session scores so far |
| `/sigmionary leaderboard` | Anyone | All-time server leaderboard |
| `/sigmionary stats [user]` | Anyone | Points, correct answers, best streak |
| `/sigmionary help` | Anyone | In-Discord how-to-play card |

*Mod = requires the **Manage Server** permission.*

---

### Leaderboard & Stats

- `/sigmionary leaderboard` — top 10 players for **this server**, ranked by total points.
- `/sigmionary stats` — your rank, total points, correct answers, games played, and best streak.
- All data is **server-specific** — scores from other servers never appear here.

---

## Adding Questions

Questions live in `questions/data.csv`. Each row needs:

| Column | Example |
|---|---|
| `#` | `5` |
| `Category` | `Kerala` |
| `Sub-category` | `Beach` |
| `Item` | `Kappil` |
| `Pics` | `Cap+Pill+Beach` *(informational only)* |

Images go in `questions/<Category>/<Item>/` and must be named with a numeric prefix that sets reveal order:

```
questions/
  Kerala/
    Kappil/
      1-cap.jpg
      2-pill.png
      3-beach.jpg
```

Both `.jpg` and `.png` are supported. The bot fuzzy-matches folder names to item names, so minor spelling differences between the CSV and folder name are fine.

---

## Setup

See [SETUP.md](SETUP.md) for step-by-step instructions on creating the Discord bot and running the server.
