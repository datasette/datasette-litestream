import { mount } from "svelte";
import App from "./App.svelte";
import { loadPageData } from "./lib/pageData";
import "./app.css";

const target = document.getElementById("app-root");
if (target) {
  mount(App, {
    target,
    props: { pageData: loadPageData() },
  });
}
