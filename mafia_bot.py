import asyncio
from collections import Counter
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
import os
import random
import threading
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import Forbidden, TelegramError
from telegram.ext import ApplicationBuilder, CallbackQueryHandler, CommandHandler, ContextTypes

BOT_TOKEN = os.environ.get("MAFIA_BOT_TOKEN", "")

MIN_PLAYERS = 4
MAX_PLAYERS = 10
NIGHT_SECONDS = 35
DISCUSS_SECONDS = 45
VOTE_SECONDS = 30

GAMES: dict[int, "MafiaGame"] = {}


@dataclass
class Player:
    user_id: int
    name: str
    role: str = ""
    alive: bool = True


@dataclass
class MafiaGame:
    chat_id: int
    chat_title: str
    host_id: int
    phase: str = "lobby"
    day_number: int = 0
    players: dict[int, Player] = field(default_factory=dict)

    mafia_targets: dict[int, int] = field(default_factory=dict)
    doctor_target: int | None = None
    detective_checked: bool = False
    night_done_event: asyncio.Event = field(default_factory=asyncio.Event)

    day_votes: dict[int, int | None] = field(default_factory=dict)
    vote_done_event: asyncio.Event = field(default_factory=asyncio.Event)

    lobby_message_id: int | None = None
    loop_task: asyncio.Task | None = None

    def alive_players(self) -> list[Player]:
        return [p for p in self.players.values() if p.alive]

    def alive_by_role(self, role: str) -> list[Player]:
        return [p for p in self.players.values() if p.alive and p.role == role]

    def check_winner(self) -> str | None:
        mafia_count = len(self.alive_by_role("Mafia"))
        town_count = len(self.alive_players()) - mafia_count
        if mafia_count == 0:
            return "Town"
        if mafia_count >= town_count:
            return "Mafia"
        return None

    def all_night_actions_done(self) -> bool:
        alive_mafia = self.alive_by_role("Mafia")
        alive_doc = self.alive_by_role("Doctor")
        alive_det = self.alive_by_role("Detective")

        mafia_ok = all(m.user_id in self.mafia_targets for m in alive_mafia)
        doc_ok = (len(alive_doc) == 0) or (self.doctor_target is not None)
        det_ok = (len(alive_det) == 0) or self.detective_checked
        return mafia_ok and doc_ok and det_ok


def lobby_text(game: MafiaGame) -> str:
    player_lines = "\n".join(f"{i}. {p.name}" for i, p in enumerate(game.players.values(), 1))
    if not player_lines:
        player_lines = "(No players yet)"
    return (
        f"MINI MAFIA — LOBBY\n\n"
        f"Players ({len(game.players)}/{MAX_PLAYERS}):\n"
        f"{player_lines}\n\n"
        f"Need at least {MIN_PLAYERS} players to start.\n"
        f"Note: Make sure you have tapped Start in a private chat with this bot before joining."
    )


def lobby_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Join Game", callback_data=f"join:{chat_id}"),
            InlineKeyboardButton("Leave", callback_data=f"leave:{chat_id}"),
        ],
        [InlineKeyboardButton("Start Game", callback_data=f"start:{chat_id}")],
    ])


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == "private":
        await update.message.reply_text(
            "You're all set! I can now send you your secret Mafia role in private.\n"
            "Go back to your group chat and tap 'Join Game'."
        )
    else:
        await update.message.reply_text("Use /newgame to start a Mini Mafia lobby in this group.")


async def cmd_newgame(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type not in ("group", "supergroup"):
        await update.message.reply_text("Add me to a group chat and run /newgame there.")
        return

    if chat.id in GAMES and GAMES[chat.id].phase != "ended":
        await update.message.reply_text("A game is already running here. Use /cancel to stop it.")
        return

    game = MafiaGame(
        chat_id=chat.id,
        chat_title=chat.title or "this group",
        host_id=update.effective_user.id,
    )
    GAMES[chat.id] = game

    msg = await update.message.reply_text(
        lobby_text(game),
        reply_markup=lobby_keyboard(chat.id),
    )
    game.lobby_message_id = msg.message_id


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    game = GAMES.get(chat.id)
    if not game:
        await update.message.reply_text("No active game in this group.")
        return

    if game.loop_task and not game.loop_task.done():
        game.loop_task.cancel()
    game.phase = "ended"
    GAMES.pop(chat.id, None)
    await update.message.reply_text("Game cancelled.")


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    parts = query.data.split(":")
    action = parts[0]
    chat_id = int(parts[1])

    game = GAMES.get(chat_id)
    if not game or game.phase == "ended":
        await query.answer("This game is no longer active.", show_alert=True)
        return

    if action == "join":
        if game.phase != "lobby":
            await query.answer("Game has already started.", show_alert=True)
            return
        if user.id in game.players:
            await query.answer("You are already in the lobby.")
            return
        if len(game.players) >= MAX_PLAYERS:
            await query.answer("Lobby is full.", show_alert=True)
            return

        try:
            await context.bot.send_message(
                chat_id=user.id,
                text=f"Joined Mini Mafia in '{game.chat_title}'. Waiting for the host to start...",
            )
        except Forbidden:
            bot_me = await context.bot.get_me()
            await query.answer(
                f"I can't DM you yet! Open @{bot_me.username}, tap Start, then tap Join Game again.",
                show_alert=True,
            )
            return

        game.players[user.id] = Player(user_id=user.id, name=user.first_name)
        await query.answer("Joined!")
        await query.edit_message_text(lobby_text(game), reply_markup=lobby_keyboard(chat_id))
        return

    if action == "leave":
        if game.phase != "lobby":
            await query.answer("Can't leave after the game starts.", show_alert=True)
            return
        if user.id not in game.players:
            await query.answer("You aren't in the lobby.")
            return
        del game.players[user.id]
        await query.answer("Left the lobby.")
        await query.edit_message_text(lobby_text(game), reply_markup=lobby_keyboard(chat_id))
        return

    if action == "start":
        if game.phase != "lobby":
            await query.answer("Already started.")
            return
        if user.id != game.host_id:
            await query.answer("Only the player who ran /newgame can start it.", show_alert=True)
            return
        if len(game.players) < MIN_PLAYERS:
            await query.answer(f"Need at least {MIN_PLAYERS} players to start.", show_alert=True)
            return

        await query.answer("Starting game...")
        await query.edit_message_text(f"Game started with {len(game.players)} players! Check your DMs for your secret role.")
        game.loop_task = asyncio.create_task(run_game_loop(game, context))
        return

    if action in ("kill", "save", "check"):
        if game.phase != "night":
            await query.answer("Night phase is already over.", show_alert=True)
            return

        actor = game.players.get(user.id)
        if not actor or not actor.alive:
            await query.answer("You are not alive in this game.", show_alert=True)
            return

        target_id = int(parts[2])
        target = game.players.get(target_id)
        if not target or not target.alive:
            await query.answer("Invalid target.", show_alert=True)
            return

        if action == "kill" and actor.role == "Mafia":
            game.mafia_targets[user.id] = target_id
            await query.answer(f"Targeted {target.name}.")
            await query.edit_message_text(f"Night {game.day_number}: You chose to eliminate {target.name}.")

        elif action == "save" and actor.role == "Doctor":
            if game.doctor_target is not None:
                await query.answer("You already made your choice.")
                return
            game.doctor_target = target_id
            await query.answer(f"Protecting {target.name}.")
            await query.edit_message_text(f"Night {game.day_number}: You chose to protect {target.name}.")

        elif action == "check" and actor.role == "Detective":
            if game.detective_checked:
                await query.answer("You already investigated someone tonight.")
                return
            game.detective_checked = True
            verdict = "MAFIA" if target.role == "Mafia" else "INNOCENT"
            await query.answer("Investigation complete.")
            await query.edit_message_text(
                f"Night {game.day_number} Investigation Result:\n{target.name} is {verdict}."
            )

        if game.all_night_actions_done():
            game.night_done_event.set()
        return

    if action == "vote":
        if game.phase != "vote":
            await query.answer("Voting is closed.", show_alert=True)
            return

        voter = game.players.get(user.id)
        if not voter or not voter.alive:
            await query.answer("Only living players can vote.", show_alert=True)
            return

        target_raw = parts[2]
        if target_raw == "skip":
            game.day_votes[user.id] = None
            await query.answer("Voted to skip.")
        else:
            target_id = int(target_raw)
            target = game.players.get(target_id)
            if not target or not target.alive:
                await query.answer("That player is already out.", show_alert=True)
                return
            game.day_votes[user.id] = target_id
            await query.answer(f"Voted for {target.name}.")

        if len(game.day_votes) >= len(game.alive_players()):
            game.vote_done_event.set()


def assign_roles(game: MafiaGame):
    player_list = list(game.players.values())
    random.shuffle(player_list)

    num_mafia = 2 if len(player_list) >= 7 else 1
    roles = ["Mafia"] * num_mafia + ["Doctor", "Detective"]
    roles += ["Villager"] * (len(player_list) - len(roles))
    random.shuffle(roles)

    for player, role in zip(player_list, roles):
        player.role = role


async def send_role_dms(game: MafiaGame, context: ContextTypes.DEFAULT_TYPE):
    mafia_names = [p.name for p in game.players.values() if p.role == "Mafia"]
    descriptions = {
        "Mafia": "Eliminate one player each night. Blend in during the day.",
        "Doctor": "Protect one player each night from being eliminated.",
        "Detective": "Investigate one player each night to learn if they are Mafia.",
        "Villager": "Find and vote out the Mafia during the day.",
    }

    for p in game.players.values():
        extra = ""
        if p.role == "Mafia" and len(mafia_names) > 1:
            teammates = [n for n in mafia_names if n != p.name]
            extra = f"\nYour Mafia teammate: {', '.join(teammates)}"
        try:
            await context.bot.send_message(
                chat_id=p.user_id,
                text=f"Game: {game.chat_title}\nYour Secret Role: {p.role.upper()}\n{descriptions[p.role]}{extra}",
            )
        except TelegramError:
            pass


async def run_night_phase(game: MafiaGame, context: ContextTypes.DEFAULT_TYPE):
    game.phase = "night"
    game.day_number += 1
    game.mafia_targets.clear()
    game.doctor_target = None
    game.detective_checked = False
    game.night_done_event.clear()

    await context.bot.send_message(
        chat_id=game.chat_id,
        text=f"NIGHT {game.day_number} BEGINS\nThe town sleeps. Mafia, Doctor, and Detective—check your DMs ({NIGHT_SECONDS}s).",
    )

    alive = game.alive_players()

    for p in alive:
        if p.role == "Mafia":
            targets = [t for t in alive if t.role != "Mafia"]
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton(t.name, callback_data=f"kill:{game.chat_id}:{t.user_id}")]
                for t in targets
            ])
            await context.bot.send_message(p.user_id, f"Night {game.day_number}: Who do you want to eliminate?", reply_markup=kb)

        elif p.role == "Doctor":
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton(t.name, callback_data=f"save:{game.chat_id}:{t.user_id}")]
                for t in alive
            ])
            await context.bot.send_message(p.user_id, f"Night {game.day_number}: Who do you want to protect?", reply_markup=kb)

        elif p.role == "Detective":
            targets = [t for t in alive if t.user_id != p.user_id]
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton(t.name, callback_data=f"check:{game.chat_id}:{t.user_id}")]
                for t in targets
            ])
            await context.bot.send_message(p.user_id, f"Night {game.day_number}: Who do you want to investigate?", reply_markup=kb)

    try:
        await asyncio.wait_for(game.night_done_event.wait(), timeout=NIGHT_SECONDS)
    except asyncio.TimeoutError:
        pass

    killed_player: Player | None = None
    if game.mafia_targets:
        counts = Counter(game.mafia_targets.values())
        top_votes = max(counts.values())
        top_targets = [tid for tid, c in counts.items() if c == top_votes]
        chosen_id = random.choice(top_targets)
        if chosen_id != game.doctor_target:
            killed_player = game.players[chosen_id]
            killed_player.alive = False

    if killed_player:
        await context.bot.send_message(
            chat_id=game.chat_id,
            text=f"DAY {game.day_number} DAWNS\n{killed_player.name} was eliminated during the night. They were a {killed_player.role}.",
        )
    else:
        await context.bot.send_message(
            chat_id=game.chat_id,
            text=f"DAY {game.day_number} DAWNS\nEveryone survived the night!",
        )


async def run_day_phase(game: MafiaGame, context: ContextTypes.DEFAULT_TYPE):
    game.phase = "discuss"
    alive = game.alive_players()
    alive_list = ", ".join(p.name for p in alive)

    await context.bot.send_message(
        chat_id=game.chat_id,
        text=f"Alive players ({len(alive)}): {alive_list}\n\nDiscuss who the Mafia is! Voting opens in {DISCUSS_SECONDS} seconds.",
    )
    await asyncio.sleep(DISCUSS_SECONDS)

    game.phase = "vote"
    game.day_votes.clear()
    game.vote_done_event.clear()

    buttons = [
        [InlineKeyboardButton(p.name, callback_data=f"vote:{game.chat_id}:{p.user_id}")]
        for p in alive
    ]
    buttons.append([InlineKeyboardButton("Skip Vote", callback_data=f"vote:{game.chat_id}:skip")])

    await context.bot.send_message(
        chat_id=game.chat_id,
        text=f"VOTING TIME ({VOTE_SECONDS}s)\nTap a player to vote them out:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )

    try:
        await asyncio.wait_for(game.vote_done_event.wait(), timeout=VOTE_SECONDS)
    except asyncio.TimeoutError:
        pass

    if not game.day_votes:
        await context.bot.send_message(game.chat_id, "No votes were cast. Nobody was eliminated.")
        return

    counts = Counter(game.day_votes.values())
    top_count = max(counts.values())
    top_choices = [choice for choice, c in counts.items() if c == top_count]

    if len(top_choices) > 1 or top_choices[0] is None:
        await context.bot.send_message(game.chat_id, "Vote was tied or skipped. Nobody was eliminated.")
        return

    voted_out = game.players[top_choices[0]]
    voted_out.alive = False
    await context.bot.send_message(
        game.chat_id,
        f"The town voted out {voted_out.name} ({top_count} votes).\nTheir role was: {voted_out.role.upper()}.",
    )


async def announce_winner(game: MafiaGame, winner: str, context: ContextTypes.DEFAULT_TYPE):
    role_summary = "\n".join(
        f"- {p.name}: {p.role} ({'Alive' if p.alive else 'Out'})"
        for p in game.players.values()
    )
    await context.bot.send_message(
        chat_id=game.chat_id,
        text=f"GAME OVER — {winner.upper()} WINS!\n\nFinal Roles:\n{role_summary}\n\nRun /newgame to play again.",
    )


async def run_game_loop(game: MafiaGame, context: ContextTypes.DEFAULT_TYPE):
    try:
        assign_roles(game)
        await send_role_dms(game, context)
        await asyncio.sleep(3)

        while True:
            await run_night_phase(game, context)
            winner = game.check_winner()
            if winner:
                await announce_winner(game, winner, context)
                break

            await run_day_phase(game, context)
            winner = game.check_winner()
            if winner:
                await announce_winner(game, winner, context)
                break

    except asyncio.CancelledError:
        pass
    finally:
        game.phase = "ended"
        GAMES.pop(game.chat_id, None)


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Mini Mafia is alive")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, format, *args):
        pass


def start_health_server():
    port = int(os.environ.get("PORT", 8080))
    HTTPServer(("0.0.0.0", port), HealthHandler).serve_forever()


def main():
    if not BOT_TOKEN:
        print("Error: MAFIA_BOT_TOKEN environment variable is not set.")
        return

    threading.Thread(target=start_health_server, daemon=True).start()

    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("newgame", cmd_newgame))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CallbackQueryHandler(handle_callback))

    print("Mini Mafia bot is online...")
    app.run_polling()


if __name__ == "__main__":
    main()
