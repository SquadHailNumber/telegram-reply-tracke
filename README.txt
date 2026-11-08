TELEGRAM BOT: SHARED SHIFT LIST

Files:
- bot.py - bot source code
- requirements.txt - dependencies (aiogram 3.4.0)
- Procfile - Bothost worker command
- runtime.txt - Python runtime

UPDATE ON BOTHOST WITHOUT DELETING OLD DATA
1. Do not delete the existing bot or its persistent storage/database.
2. Replace only bot.py in the existing GitHub repository with this version.
3. Keep requirements.txt, Procfile and runtime.txt as they are.
4. In Bothost, use the existing project and existing persistent storage. Click update/redeploy from Git.
5. Do not change DB_PATH. Existing records are migrated automatically and retained.

CONNECT THE SECOND PERSON
1. Open the bot from your own Telegram account and press Start.
2. Open the bot menu and tap "Совместный доступ".
3. The bot will show a one-time command such as /join A1B2C3D4.
4. Send that command privately to the second person.
5. The second person opens the same bot, presses Start, then sends /join CODE.
6. The bot confirms that the shared list is connected. Both users now have the same permissions and see the same list.

IMPORTANT
- Invite code is one-time. If it is used by the wrong person, create a new one.
- The shared workspace supports up to two Telegram accounts.
- Do not publish or forward the invite code to anyone else.
- Existing records are assigned to the Telegram account that owned them before migration.
- The second person should use /join CODE before trying list/add commands.
- Keep BOT_TOKEN and DB_PATH settings in Bothost unchanged.
