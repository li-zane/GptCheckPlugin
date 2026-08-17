import { createRoot } from "react-dom/client";

import App from "./App";
import "./shared/styles/tokens.css";
import "./shared/styles/global.css";
import "./shared/styles/legacy.css";

createRoot(document.getElementById("root")!).render(
  <App />,
);
