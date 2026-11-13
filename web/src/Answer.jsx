import React from "react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";

// Answers quote repository text, so an answer is untrusted content. Raw HTML stays
// inert (react-markdown does not render it), images are dropped so nothing can fetch
// a URL just by being displayed, and links become plain text showing where they
// point, so injected text cannot carry data out or send a reader somewhere.
const PLUGINS = [remarkGfm];
const NO_IMAGES = ["img"];
const COMPONENTS = {
  a({ children, href }) {
    const text = String(children);
    return <span>{text}{href && href !== text ? ` (${href})` : ""}</span>;
  },
};

export default function Answer({ text }) {
  return (
    <div className="md">
      <Markdown remarkPlugins={PLUGINS} disallowedElements={NO_IMAGES} components={COMPONENTS}>
        {text}
      </Markdown>
    </div>
  );
}
