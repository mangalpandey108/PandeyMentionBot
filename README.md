# Pandey Mention Bot

## Commands
- `/start` - start/info
- `/help` - command help
- `/mention` - mention group members
- `/mention your custom message` - send custom text with member mentions
- `/cancel` - stop the running mention/question sending job
- `/users` - member count
- `/id` - chat and user IDs
- `/question` - send a different Hindi quiz question to each member, about one second apart

Admin-only: `/mention`, `/cancel`, `/question`.

## FadeHost setup
1. Select Python 3.11.
2. Connect the GitHub repository and deploy the `main` branch.
3. Install requirements from `requirements.txt` (if the host has a build/install command, use `pip install -r requirements.txt`).
4. Start command: `python main.py`.
5. Add environment variables in the host panel: `API_ID`, `API_HASH`, `BOT_TOKEN`.
6. Add the bot to the group and promote it to administrator with permission to read messages and send messages.

Never upload `.env` or session files to GitHub.

## Quiz bank
Edit `questions.json` to add questions. Each item has `category`, `question`, and `answers` (a list of accepted answers).
Current-affairs questions must be refreshed regularly from reliable, up-to-date sources; the starter bank does not pretend that old facts are current.
