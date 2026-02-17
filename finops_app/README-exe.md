# Azure MACC Analyst — Desktop Distribution

## For the IT person building the package

### Prerequisites
- Python 3.11+ on the build machine
- Internet access (to download dependencies)

### Build steps
1. Open PowerShell in `finops_app/`
2. Run:
   ```powershell
   powershell -ExecutionPolicy Bypass -File build_exe.ps1
   ```
3. When finished you'll see:
   ```
   dist/AzureMaccAnalyst.zip   ← share this file
   ```

### What the build produces
| Path | Description |
|------|-------------|
| `dist/AzureMaccAnalyst/` | Folder containing the `.exe` and all runtime files |
| `dist/AzureMaccAnalyst.zip` | ZIP of the above — **this is what you share** |

---

## For the finance user

### Getting started
1. **Download** `AzureMaccAnalyst.zip` (shared by your IT contact)
2. **Right-click → Extract All…** to a folder on your desktop
3. Open the extracted `AzureMaccAnalyst` folder
4. **Double-click `AzureMaccAnalyst.exe`**
5. A console window appears briefly, then your **browser opens** to the dashboard
6. Click **Sign in to Azure** and follow the normal workflow

### Requirements
- Windows 10/11
- Internet access (to reach Azure APIs and Microsoft sign-in)
- Your Microsoft account must have **Cost Management Reader** (or Reader) on the relevant Azure subscriptions
- No Python installation needed — everything is included

### Tips
- **First launch** may take 10-15 seconds — the app is starting a local web server.
- If your browser doesn't open automatically, navigate to `http://localhost:8501`.
- To stop the app, close the console window.
- If port 8501 is in use, Streamlit will pick another port — check the console output.

### Troubleshooting
| Issue | Fix |
|-------|-----|
| Windows SmartScreen blocks the app | Click **More info → Run anyway** (the app is unsigned) |
| Browser shows "can't reach this page" | Wait a few more seconds; the server is still starting |
| Azure sign-in fails | Ensure you can sign in at https://portal.azure.com with the same account |
| "Access denied" on cost data | Ask your Azure admin to grant Cost Management Reader role |

---

## For enterprise deployment
- **Code-sign** the `.exe` with your organisation's certificate to avoid SmartScreen warnings.
- Consider distributing via an internal file share or SharePoint.
- The app only makes outbound HTTPS calls to `management.azure.com` and `login.microsoftonline.com`.
