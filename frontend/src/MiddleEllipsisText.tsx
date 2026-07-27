import { useLayoutEffect, useRef, useState } from "react";

const ELLIPSIS = "...";

function middleCandidate(value: string, visibleCharacters: number) {
  const leading = Math.ceil(visibleCharacters / 2);
  const trailing = Math.max(1, visibleCharacters - leading);
  return value.slice(0, leading) + ELLIPSIS + value.slice(-trailing);
}

export function fitMiddleEllipsis(
  value: string,
  availableWidth: number,
  measure: (candidate: string) => number,
) {
  if (!value || availableWidth <= 0 || measure(value) <= availableWidth) return value;
  if (measure(ELLIPSIS) > availableWidth) return ELLIPSIS;

  let low = 2;
  let high = Math.max(2, value.length - 1);
  let best = ELLIPSIS;
  while (low <= high) {
    const visibleCharacters = Math.floor((low + high) / 2);
    const candidate = middleCandidate(value, visibleCharacters);
    if (measure(candidate) <= availableWidth) {
      best = candidate;
      low = visibleCharacters + 1;
    } else {
      high = visibleCharacters - 1;
    }
  }
  return best;
}

function elementTextMeasure(element: HTMLElement) {
  const canvas = document.createElement("canvas");
  const context = canvas.getContext("2d");
  const style = window.getComputedStyle(element);
  if (!context) return (candidate: string) => candidate.length * 8;

  context.font = [style.fontStyle, style.fontVariant, style.fontWeight, style.fontSize, style.fontFamily]
    .filter(Boolean)
    .join(" ");
  const letterSpacing = Number.parseFloat(style.letterSpacing) || 0;
  return (candidate: string) => (
    context.measureText(candidate).width + Math.max(0, candidate.length - 1) * letterSpacing
  );
}

export function MiddleEllipsisText({ className, text }: { className?: string; text: string }) {
  const elementRef = useRef<HTMLSpanElement>(null);
  const [visibleText, setVisibleText] = useState(text);

  useLayoutEffect(() => {
    const element = elementRef.current;
    if (!element) return undefined;

    const update = () => {
      const fitted = fitMiddleEllipsis(text, element.clientWidth, elementTextMeasure(element));
      setVisibleText((current) => current === fitted ? current : fitted);
    };
    update();

    if (typeof ResizeObserver === "undefined") {
      window.addEventListener("resize", update);
      return () => window.removeEventListener("resize", update);
    }
    const observer = new ResizeObserver(update);
    observer.observe(element);
    return () => observer.disconnect();
  }, [text]);

  return (
    <span
      aria-label={text}
      className={["middle-ellipsis-text", className].filter(Boolean).join(" ")}
      ref={elementRef}
    >
      {visibleText}
    </span>
  );
}
