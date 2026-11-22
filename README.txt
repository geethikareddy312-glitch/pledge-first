PLEDGE FIRST: how to run it (no Python knowledge needed)

1. Install Python (one time only)
   - Go to https://www.python.org/downloads/ and click the yellow Download button.
   - Open the downloaded file. On the FIRST screen, tick "Add python.exe to PATH" (bottom of the window).
   - Click "Install Now" and wait until it says "Setup was successful".

2. Unzip this folder
   - Right-click the zip file and choose "Extract All", then open the new folder.

3. Start the website
   - Windows: double-click  start_windows.bat
   - Mac:     open Terminal, type  bash  then a space, drag start_mac_linux.sh into the window, press Enter.
   The first time it installs a small component (about a minute). A black window opens and your
   browser opens the website at http://localhost:5000
   Keep the black window open while you use the website. Closing it stops the website.

4. Stop the website: close the black window (or press Ctrl+C in it).

5.5. College demo features
   - Smart notifications for new pledges, goal reached, and products marked ready.
   - Improved experimental AI demand estimates using the founder's completed campaign history when available.
   - Customer recommendations based on categories they have previously supported (or the popular demo category).
   - Demo checkout offers UPI demo, Card demo, and Cash on delivery demo choices. It records a simulated receipt only.
     It does not connect to a payment gateway, collect card/bank details, or charge real money.

6. Founder analytics
   - Create or sign in with a Business owner account, then open AI analytics in the navigation bar.
   - The dashboard summarizes your own products and real pledge records, including a 7-day trend.
   - AI demand estimates are experimental: the current logistic-regression model was trained on
     synthetic data, so do not treat its percentages as validated forecasts. Demo payments are simulated.

7. Start the demo from scratch: stop the website, delete the file  pledge_first.db  in this folder,
   then start it again. (All accounts and pledges you made are erased and the sample products come back.)

If Windows says "Windows protected your PC": click "More info", then "Run anyway".
If the page does not open: type  localhost:5000  in your browser's address bar.


SMART NOTIFICATIONS
- Customers receive in-app updates when a pledged product reaches its goal and when it is marked ready.
- Business owners receive an in-app update whenever someone pledges for one of their products.
- Use the Notifications button in the top navigation to view updates and mark one or all as read.
- Notifications refresh automatically while the website is open.
- Existing databases are upgraded automatically with a read-status field; existing accounts and pledges are preserved.


AI PREDICTION IMPROVEMENT
- Active campaign estimates now blend the original synthetic-data model with the signed-in founder's completed campaign outcomes in the same category.
- A smoothing prior reduces overreaction when only a few campaigns have completed.
- The dashboard explains how many past campaigns informed each estimate. With no relevant history, it clearly falls back to the experimental baseline.
- These are still experimental estimates, not validated forecasts. Collect more real campaign outcomes and evaluate model performance before making business decisions.


COLLEGE DEMO PAYMENT FLOW
- A customer pledges to a product while it is collecting; no payment is requested then.
- When the goal is reached, the founder can mark the product as manufactured from Founder dashboard.
- The customer then sees a Pay button and can choose one of three clearly labelled demo methods.
- Completing checkout stores a demo payment method and reference number in the local SQLite database.
- No real transaction occurs. Do not enter real payment credentials; the demo does not ask for them.

- Customer recommendations use the category the customer has backed most often, with a popular-category fallback.
