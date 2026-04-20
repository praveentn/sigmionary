Sigmionary is a pycord discord game bot.

Multi-player game where group of images are shown along with the category and subcategory. Users have to guess the item - similar to pictionary game.

Instructions:
a. Sample data is provided in questions/data.csv file.
Images are placed under questions/<category_name> folder.
Image names follows <number>-<what the image is>.
Always show images in the order in their names.
eg. Kerala/Kappil/1-cap.png
Kerala/Kappil/2-pill.jpg
Kerala/Kappil/3-beach.jpg

b. Show 1. cap 2. pill 3. beach in this order
Ensure images are fit into similar sized windows - ensure superior user experience in displaying images.

c. Users can type in their responses as raw texts (no commands required). Always use fuzzy matching to check the accuracy of users' entries. Kaappil and Kappil are same for example. Use appropriate thresholds for fuzzy matches.

d. Make the game an addictive and enriching experience. Stack with points, streak bonus, timings, accuracy, leaderboards etc.

e. Remember that images could be jpg or png.

Guidelines:
1. ensure server-level isolation is there for all commands. user details from one server shouldn't show up in another. leaderboard, stats, and other game details should be server-level always.

2. validate commands and corresponding accesses

3. think from a pen-tester perspective and ensure app is secure

4. your code will be reviewed by the best coder in the world, so ensure you stay ahead of the game

5. Refer references/common-errors.txt to see a list of known issues

6. Refer references/bot.py to follow the structure and elements of the code required bot.py file. It's the bot file for another deployed game currently live.

7. Refer references/slash_commands.md file to follow best practices for slash commands. let all commands start with sigmionary <> <>

8. Create a start.bat and sh file same as references/start.bat file
