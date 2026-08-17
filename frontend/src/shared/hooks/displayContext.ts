import { createContext } from "react";

export const defaultDisplayTimeZone = "Asia/Shanghai";
export const TimeZoneContext = createContext(defaultDisplayTimeZone);
export const NowContext = createContext(Date.now());
