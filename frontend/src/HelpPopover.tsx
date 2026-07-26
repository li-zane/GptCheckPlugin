import { CircleHelp } from "lucide-react";
import {
  useCallback,
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { createPortal } from "react-dom";

type PopoverPosition = {
  left: number;
  top: number;
  width: number;
};

export function HelpPopover({
  children,
  label = "查看说明",
  trigger,
  triggerClassName = "",
}: {
  children: ReactNode;
  label?: string;
  trigger?: ReactNode;
  triggerClassName?: string;
}) {
  const [open, setOpen] = useState(false);
  const [pinned, setPinned] = useState(false);
  const [position, setPosition] = useState<PopoverPosition>({ left: 12, top: 12, width: 310 });
  const contentId = useId();
  const rootRef = useRef<HTMLSpanElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const contentRef = useRef<HTMLSpanElement>(null);

  const close = useCallback((blurTrigger = false) => {
    setOpen(false);
    setPinned(false);
    if (blurTrigger) triggerRef.current?.blur();
  }, []);

  useLayoutEffect(() => {
    if (!open) return;
    const updatePosition = () => {
      const trigger = triggerRef.current;
      const content = contentRef.current;
      if (!trigger || !content) return;
      const margin = 12;
      const gap = 6;
      const viewportWidth = document.documentElement.clientWidth;
      const viewportHeight = document.documentElement.clientHeight;
      const width = Math.min(310, Math.max(180, viewportWidth - margin * 2));
      const triggerRect = trigger.getBoundingClientRect();
      const contentHeight = content.getBoundingClientRect().height;
      const left = Math.min(
        Math.max(margin, triggerRect.right - width),
        Math.max(margin, viewportWidth - width - margin),
      );
      const belowTop = triggerRect.bottom + gap;
      const top = belowTop + contentHeight <= viewportHeight - margin
        ? belowTop
        : Math.max(margin, triggerRect.top - contentHeight - gap);
      setPosition({ left, top, width });
    };
    updatePosition();
    window.addEventListener("resize", updatePosition);
    window.addEventListener("scroll", updatePosition, true);
    return () => {
      window.removeEventListener("resize", updatePosition);
      window.removeEventListener("scroll", updatePosition, true);
    };
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const closeOnOutsidePress = (event: PointerEvent) => {
      const target = event.target as Node;
      if (!rootRef.current?.contains(target) && !contentRef.current?.contains(target)) close(true);
    };
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        close(true);
      }
    };
    document.addEventListener("pointerdown", closeOnOutsidePress);
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.removeEventListener("pointerdown", closeOnOutsidePress);
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, [close, open]);

  return (
    <span
      className={`help-popover${open ? " is-open" : ""}`}
      onMouseEnter={() => setOpen(true)}
      onMouseLeave={() => {
        if (!pinned) setOpen(false);
      }}
      ref={rootRef}
    >
      <button
        aria-describedby={open ? contentId : undefined}
        aria-expanded={open}
        aria-label={label}
        className={`help-popover-trigger${triggerClassName ? ` ${triggerClassName}` : ""}`}
        onBlur={() => {
          if (!pinned) setOpen(false);
        }}
        onClick={(event) => {
          event.preventDefault();
          event.stopPropagation();
          const nextPinned = !pinned;
          setPinned(nextPinned);
          setOpen(nextPinned);
          if (!nextPinned) event.currentTarget.blur();
        }}
        onFocus={() => setOpen(true)}
        ref={triggerRef}
        title={label}
        type="button"
      >
        {trigger ?? <CircleHelp size={15} />}
      </button>
      {open
        ? createPortal(
          <span
            className="help-popover-content is-visible"
            id={contentId}
            ref={contentRef}
            role="tooltip"
            style={position}
          >
            {children}
          </span>,
          document.body,
        )
        : null}
    </span>
  );
}
