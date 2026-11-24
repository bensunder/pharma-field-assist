import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { Markdown } from "./markdown";

const html = (text: string) => renderToStaticMarkup(<Markdown text={text} />);

describe("Markdown", () => {
  it.each([
    ["**bold**", "<strong>bold</strong>"],
    ["*em*", "<em>em</em>"],
    ["`code`", "<code>code</code>"],
    ["dose [1]", '<sup class="cite">[1]</sup>'],
  ])("renders %s", (input, expected) => {
    expect(html(input)).toContain(expected);
  });

  it("renders bullet and numbered lists", () => {
    expect(html("- a\n- b")).toBe("<ul><li>a</li><li>b</li></ul>");
    expect(html("1. a\n2. b")).toBe("<ol><li>a</li><li>b</li></ol>");
    expect(html("Lead:\n- a\n- b")).toBe("<p>Lead:</p><ul><li>a</li><li>b</li></ul>");
  });

  it("never injects HTML from model output", () => {
    const out = html('<img src=x onerror="alert(1)"> **<script>x</script>**');
    expect(out).not.toContain("<img");
    expect(out).not.toContain("<script>");
    expect(out).toContain("&lt;script&gt;");
  });
});

describe("Markdown identifiers", () => {
  it("leaves snake_case identifiers intact", () => {
    expect(html("*Source: gold.weekly_rx_by_territory*")).toBe("<p><em>Source: gold.weekly_rx_by_territory</em></p>");
  });
});
