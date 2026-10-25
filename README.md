# Step-by-Step Guide: How to Fork, Clone, and Submit Your Assessment

**Repository:** https://github.com/savvpro/savvpro-test-voice-agent

---

## Step 1: Fork the Repository

1. Go to the repository: https://github.com/savvpro/savvpro-test-voice-agent
2. Click the **Fork** button in the top-right corner.
3. This will create a copy of the repository in your GitHub account.

---

## Step 2: Clone Your Fork

1. Open your forked repository in your GitHub account.
2. Click the green **Code** button and copy the URL.
3. Run the following commands:

```bash
git clone https://github.com/your-username/savvpro-test-voice-agent.git
cd savvpro-test-voice-agent
```

---

## Step 3: Read the Task

Open and read **TASK.md** carefully before writing any code.

- TASK.md URL: https://github.com/savvpro/savvpro-test-voice-agent/blob/main/TASK.md

---

## Step 4: Create a New Branch

Branch pattern: `candidate-<your-github-username>`

```bash
git checkout -b candidate-<your-github-username>
```

**Example:**
```bash
git checkout -b candidate-johndoe
```

---

## Step 5: Complete the Task

Build the full application on your branch.

---

## Step 6: Push Your Branch

```bash
git add .
git commit -m "feat: complete voice-agent assessment"
git push origin candidate-<your-github-username>
```

---

## Step 7: Create a Pull Request

1. Go to your **forked** repository on GitHub (`https://github.com/your-username/savvpro-test-voice-agent`).
2. Click **Compare & pull request** (GitHub will show this banner after you push your branch).
3. Ensure the following settings in the PR form:
   - **Base repository:** `savvpro/savvpro-test-voice-agent` ← the original repo
   - **Base branch:** `main`
   - **Head repository:** `your-username/savvpro-test-voice-agent` ← your fork
   - **Compare branch:** `candidate-<your-github-username>`
4. Use the title: `feat: complete voice-agent assessment`
5. Submit the pull request.

> 💡 **Tip:** If you don't see the banner, go to the original repo (`savvpro/savvpro-test-voice-agent`), click the **Pull requests** tab, then click **New pull request** → **compare across forks**.

---

> ⚠️ **Important:** No submissions will be accepted after the deadline.
