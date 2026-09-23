import { act, fireEvent, render, screen } from "@testing-library/react";
import { vi } from "vitest";
import { ToastProvider, useToast } from "./Toasts";

function Trigger() {
  const toast = useToast();
  return (
    <>
      <button onClick={() => toast("warning", "Row count dropped")}>warn</button>
      <button onClick={() => toast("critical", "File failed")}>fail</button>
    </>
  );
}

afterEach(() => vi.useRealTimers());

it("fades warnings after eight seconds but keeps critical toasts until dismissed", () => {
  vi.useFakeTimers();
  render(<ToastProvider><Trigger /></ToastProvider>);
  fireEvent.click(screen.getByText("warn"));
  fireEvent.click(screen.getByText("fail"));
  act(() => vi.advanceTimersByTime(8000));
  expect(screen.queryByText("Row count dropped")).toBeNull();
  expect(screen.getByText("File failed")).toBeInTheDocument();
});

it("keeps only the four latest toasts", () => {
  render(<ToastProvider><Trigger /></ToastProvider>);
  for (let i = 0; i < 5; i++) fireEvent.click(screen.getByText("fail"));
  expect(screen.getAllByText("File failed")).toHaveLength(4);
});

it("does nothing outside a provider", () => {
  render(<Trigger />);
  fireEvent.click(screen.getByText("fail"));
  expect(screen.queryByText("File failed")).toBeNull();
});
