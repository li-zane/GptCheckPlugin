import { StrictMode } from "react";

import { AppProviders } from "./app/AppProviders";

export default function App() {
  return (
    <StrictMode>
      <AppProviders />
    </StrictMode>
  );
}

