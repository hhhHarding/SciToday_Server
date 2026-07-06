import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";
import DigestCard from "./DigestCard.vue";
import type { Digest } from "../types";

const digest: Digest = {
  filename: "paper.html",
  timestamp: "20260706_123456",
  title: "Paper",
  cn_title: "论文",
  keywords: "AI",
  journal: "Nature",
  source: "rss",
  preview: "摘要",
  disliked: false,
  interested: false,
  is_read: false,
  relevance_score: 80,
  novelty_score: 70,
  final_score: 77,
  recommendation_type: "ai",
};

describe("DigestCard", () => {
  it("renders App metadata and emits mutually exclusive preference intent", async () => {
    const wrapper = mount(DigestCard, { props: { digest } });
    expect(wrapper.text()).toContain("论文");
    expect(wrapper.text()).toContain("Nature");
    expect(wrapper.text()).toContain("AI推荐");
    const interested = wrapper
      .findAll("button")
      .find((button) => button.text().includes("感兴趣"));
    await interested?.trigger("click");
    expect(wrapper.emitted("flags")?.[0]).toEqual([
      digest,
      { interested: true },
    ]);
  });
});
