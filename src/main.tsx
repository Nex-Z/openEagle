import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import { installFrontendLogMirror } from "./lib/frontendLogMirror";
import "./styles.css";

installFrontendLogMirror();

ReactDOM.createRoot(document.getElementById("root") as HTMLElement).render(
  <App />,
);
