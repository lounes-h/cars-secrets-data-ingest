import { fireEvent, render, screen } from "@testing-library/react";
import { Pager, RunStatusBadge, SeverityBadge } from "./ui";

describe("status badges", () => {
  it("always pair the status color with a word", () => {
    render(<><SeverityBadge severity="critical" /><RunStatusBadge status="failed" />
      <RunStatusBadge status="missed" /></>);
    expect(screen.getByText("Critical")).toBeInTheDocument();
    expect(screen.getByText("failed")).toBeInTheDocument();
    expect(screen.getByText("missed")).toBeInTheDocument();
    expect(document.querySelectorAll("svg.icon")).toHaveLength(3);
  });
});

describe("Pager", () => {
  it("steps back a page and hides when everything fits", () => {
    const onChange = vi.fn();
    const { rerender, container } = render(<Pager offset={50} limit={50} total={120} onChange={onChange} />);
    expect(screen.getByText("Page 2 of 3")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Previous" }));
    expect(onChange).toHaveBeenCalledWith(0);
    rerender(<Pager offset={0} limit={50} total={50} onChange={onChange} />);
    expect(container).toBeEmptyDOMElement();
  });
});
