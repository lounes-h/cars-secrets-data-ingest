import { render, screen } from "@testing-library/react";
import { RunStatusBadge, SeverityBadge } from "./ui";

describe("status badges", () => {
  it("always pair the status color with a word", () => {
    render(<><SeverityBadge severity="critical" /><RunStatusBadge status="failed" /></>);
    expect(screen.getByText("Critical")).toBeInTheDocument();
    expect(screen.getByText("failed")).toBeInTheDocument();
    expect(document.querySelectorAll("svg.icon")).toHaveLength(2);
  });
});
