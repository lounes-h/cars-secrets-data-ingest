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

  it("steps back with the button and falls back to whatever size a photo has", () => {
    render(<Gallery images={[
      { thumb: "t0", full: null, hd: "h0" },
      { thumb: null, full: "f1", hd: null },
      { thumb: "t2", full: null, hd: null },
    ]} alt="lot" />);
    const main = () => screen.getByAltText(/lot, photo/) as HTMLImageElement;
    expect(main().getAttribute("src")).toBe("h0");
    fireEvent.click(screen.getByLabelText("Previous photo"));
    expect(main().getAttribute("src")).toBe("t2");
    expect(screen.getByLabelText("Photo 2").querySelector("img")?.getAttribute("src")).toBe("f1");
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

  it("links to the auction site when only part of the photos is available", () => {
    const one = [{ thumb: "https://cs.copart.com/t_thb.jpg", full: "https://cs.copart.com/t_ful.jpg", hd: null }];
    render(<Gallery images={one} alt="x" complete={false} moreUrl="https://www.copart.com/lot/63265186" />);
    const link = screen.getByText(/See all photos/);
    expect(link).toHaveAttribute("href", "https://www.copart.com/lot/63265186");
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
    expect(screen.getByText("Only this photo is available here.")).toBeInTheDocument();
  });

  it("shows no link when the photo list is complete", () => {
    render(<Gallery images={images} alt="x" complete moreUrl="https://www.copart.com/lot/1" />);
    expect(screen.queryByText(/See all photos/)).toBeNull();
  });

  it("offers the link even when there is no photo at all", () => {
    render(<Gallery images={[]} alt="x" complete={false} moreUrl="https://www.copart.com/lot/1" />);
    expect(screen.getByText("No photos for this lot.")).toBeInTheDocument();
    expect(screen.getByText(/See all photos/)).toBeInTheDocument();
  });
});
