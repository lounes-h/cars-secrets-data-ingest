import { fireEvent, render, screen } from "@testing-library/react";
import { Gallery } from "./Gallery";

const images = [
  { thumb: "https://cs.copart.com/a_thb.jpg", full: "https://cs.copart.com/a_ful.jpg", hd: "https://cs.copart.com/a_hrs.jpg" },
  { thumb: "https://cs.copart.com/b_thb.jpg", full: "https://cs.copart.com/b_ful.jpg", hd: null },
];

describe("Gallery", () => {
  it("shows the full-size photo and navigates with buttons, thumbnails and keys", () => {
    render(<Gallery images={images} alt="2013 ACURA TSX" />);
    const main = () => screen.getByAltText(/2013 ACURA TSX, photo/) as HTMLImageElement;
    expect(main().src).toBe(images[0].full);
    expect(screen.getByText("Open HD")).toHaveAttribute("rel", "noopener noreferrer");

    fireEvent.click(screen.getByLabelText("Next photo"));
    expect(main().src).toBe(images[1].full);
    expect(screen.queryByText("Open HD")).toBeNull(); // no HD for this one

    fireEvent.click(screen.getByLabelText("Photo 1"));
    expect(main().src).toBe(images[0].full);

    fireEvent.keyDown(screen.getByLabelText("Lot photos"), { key: "ArrowLeft" });
    expect(main().src).toBe(images[1].full); // wraps around
  });

  it("says so when there are no photos", () => {
    render(<Gallery images={[]} alt="x" />);
    expect(screen.getByText("No photos for this lot.")).toBeInTheDocument();
  });

  it("falls back to the thumbnail when that is all there is", () => {
    render(<Gallery images={[{ thumb: "https://cs.copart.com/t.jpg", full: null, hd: null }]} alt="x" />);
    expect((screen.getByAltText("x, photo 1 of 1") as HTMLImageElement).src).toBe("https://cs.copart.com/t.jpg");
    expect(screen.queryByLabelText("Next photo")).toBeNull();
  });
});
