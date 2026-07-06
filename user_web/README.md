# SciToday User Web

Vue 3 + TypeScript frontend served by the backend at `/user/`.

Development:

```powershell
npm install
npm run dev
```

Production build:

```powershell
.\build.ps1
```

The backend serves `user_web/dist`. The build output and `node_modules` are not
committed. Build before creating a release or copying the backend to a server.
The production frontend and `/api` must use the same HTTPS origin.
