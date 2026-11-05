import "@fontsource-variable/hanken-grotesk";
import "@fontsource-variable/jetbrains-mono";
import React from "react";
import { createRoot } from "react-dom/client";
import App from "./App.jsx";
import { applyTheme, loadTheme } from "./themes.js";
import "./styles.css";

applyTheme(loadTheme()); // before first paint, so the page never flashes unthemed
createRoot(document.getElementById("root")).render(<App />);
