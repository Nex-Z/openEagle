import React from "react";
import ReactDOM from "react-dom/client";
import { SoloOverlayWindow } from "./components/solo/SoloOverlayWindow";
import "./styles.css";

ReactDOM.createRoot(document.getElementById("app") as HTMLElement).render(
  <SoloOverlayWindow />,
);
