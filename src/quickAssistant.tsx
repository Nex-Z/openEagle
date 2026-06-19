import React from "react";
import ReactDOM from "react-dom/client";
import { QuickAssistantWindow } from "./components/quick/QuickAssistantWindow";
import "./styles.css";

ReactDOM.createRoot(document.getElementById("app") as HTMLElement).render(
  <QuickAssistantWindow />,
);
