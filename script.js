const BUY_URL = ""; // Paste your Lemon Squeezy checkout URL between the quotes.
const buyButtons = document.querySelectorAll("[data-buy-crawler]");
for (const button of buyButtons) {
  if (BUY_URL.trim()) {
    button.href = BUY_URL;
    button.target = "_blank";
    button.rel = "noopener";
  } else {
    button.href = "#crawler";
    button.textContent = "Checkout coming soon";
    button.classList.add("btn-disabled");
    button.addEventListener("click", (event) => {
      event.preventDefault();
      alert("The $4.99 checkout link will be added after the Lemon Squeezy product is created.");
    });
  }
}
document.getElementById("year").textContent = new Date().getFullYear();