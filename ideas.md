# 数据受限下的最优重复策略：基于 Quantization Model 的理论推导

基于 Michaud et al. (2023) 的 Quantization Model 和 Dai & Zheng (2026) 的 Extended Linear Model，推导数据受限多域场景下的最优重复次数。

---

## 一、问题设定

多域预训练场景。域 $i$（$i = 1, \ldots, K$）拥有有限的唯一数据 $D_i$ 个 token。总训练 token 预算为 $T$，每个域的数据可被重复 $r_i \geq 1$ 次，满足

$$\sum_{i=1}^K r_i D_i = T$$

**问题**：如何选择 $\{r_i\}$ 使目标分布 $w = (w_1, \ldots, w_K)$ 上的加权损失 $\mathcal{J} = \sum_i w_i L_i$ 最小？

---

## 二、核心论点

**命题**：在数据受限场景下（$D_i$ 为瓶颈而非算力），重复训练有两个分离的效应：

1. **可学习 quanta 数量 $n_i$ 不受重复次数影响**——由唯一数据 $D_i$ 的信息量决定；
2. **已学习 quanta 的残余损失 $a_k(r_i)$ 随重复次数递减**——SGD 优化噪声被压制，使每个 quantum 的学习质量提升、loss 改进增大。

下面从 per-quantum 模型出发，逐步推导域级损失公式和最优重复策略。

---

## 三、Per-quantum 噪声模型

### 3.1 可学习 quanta 的信息瓶颈

域 $i$ 中 quanta 的频率服从 Zipf 分布：$p_k^{(i)} = k^{-(\alpha_i + 1)} / \zeta(\alpha_i + 1)$。Quantum $k$ 要被学会，需要在唯一数据中出现足够多次：$D_i p_k^{(i)} \geq \tau_i$。可学习 quanta 的数量上限：

$$n_i = \left(\frac{D_i}{\tau_i \, \zeta(\alpha_i + 1)}\right)^{1/(\alpha_i + 1)}$$

$n_i$ 仅取决于 $D_i$，与重复次数 $r_i$ 无关。这里隐含的假设是：训练轮数 $r_i$ 已足以使所有信息充分的 quanta 收敛（即优化不是瓶颈）——这恰好是"数据受限"的含义。

### 3.2 已学习 quantum 的残余损失

在标准 Quantization Model 中，学习是二值的：quantum $k$ 学会后，其损失贡献从 $b$（未学会）降至 $a$（已学会），改进为 $\Delta = b - a$。

现实中学习并非完美。模型通过 SGD 学习 quantum $k$ 时，经过 $r_i D_i p_k$ 次梯度更新（$D_i p_k$ 个唯一样本，每个重复 $r_i$ 次）。学习后的残余损失可分解为：

$$a_k(r_i) = \underbrace{a^*}\_{Bayes\ 最优} + \underbrace{\frac{g\_{\mathrm{gen}}}{(D_i p_k)^{\beta_g}}}\_{泛化误差} + \underbrace{\frac{g\_{\mathrm{opt}}}{(r_i D_i p_k)^{\beta_o}}}\_{优化噪声}$$

- **Bayes 最优** $a^*$：即使无限数据、完美优化也无法消除的固有不确定性；
- **泛化误差**：仅取决于唯一样本数 $D_i p_k$，重复无法降低；
- **优化噪声**：取决于总梯度更新数 $r_i D_i p_k$，重复可以压制。

将前两项合并为与 $r_i$ 无关的基线 $\tilde{a}_k$，得到简化形式：

$$\boxed{a_k(r_i) = \tilde{a}_k + \frac{g}{(r_i D_i p_k)^{\beta}}}$$

其中 $\beta \in (0, 1]$ 反映重复数据的噪声衰减效率（$\beta = 1$ 对应独立样本的理想情形，$\beta < 1$ 反映重复数据的相关性导致的衰减放缓）。

**关键推论**：学会 quantum $k$ 后的 loss 改进为

$$\Delta_k(r_i) = b - a_k(r_i) = (b - \tilde{a}_k) - \frac{g}{(r_i D_i p_k)^\beta}$$

$\Delta_k(r_i)$ 随 $r_i$ 单调递增——**重复使每个 quantum 的 loss 改进增大**。

---

## 四、域级损失公式

### 4.1 聚合推导

域 $i$ 的损失由已学习和未学习两部分构成：

$$L_i = \underbrace{\sum_{k=1}^{n_i} p_k \, a_k(r_i)}\_{\text{已学习 quanta}} + \underbrace{\sum_{k > n_i} p_k \, b}\_{\text{未学习 quanta}}$$

代入 $a_k(r_i)$ 并分别处理各项。

**未学习部分**（标准结果）：

$$\sum_{k > n_i} p_k \, b \approx \frac{b}{\alpha_i \, \zeta(\alpha_i+1)} \, n_i^{-\alpha_i} = C_i' \, D_i^{-\alpha_{D,i}}$$

其中 $\alpha_{D,i} = \alpha_i / (\alpha_i + 1)$ 是数据 scaling 指数。

**已学习部分的基线**：$\sum_{k=1}^{n_i} p_k \, \tilde{a}_k \approx \tilde{a}$（归入可约常数）。

**已学习部分的噪声项**：

$$\frac{g}{(r_i D_i)^\beta} \sum_{k=1}^{n_i} p_k^{1-\beta} = \frac{g}{(r_i D_i)^\beta \, \zeta^{1-\beta}} \sum_{k=1}^{n_i} k^{-(\alpha_i+1)(1-\beta)}$$

令 $\gamma = (\alpha_i+1)(1-\beta)$。当 $\beta > \alpha_{D,i}$（即 $\gamma < 1$，SGD 收敛速率大于数据 scaling 速率——对绝大多数实际场景成立）：

$$\sum_{k=1}^{n_i} k^{-\gamma} \approx \frac{n_i^{1-\gamma}}{1 - \gamma}$$

代入 $n_i \propto D_i^{1/(\alpha_i+1)}$ 后，噪声项化为：

$$\text{Noise}_i \propto r_i^{-\beta} \, D_i^{-\alpha_{D,i}}$$

与未学习部分具有相同的 $D_i$ 指数。

### 4.2 统一公式

$$\boxed{L_i(D_i, r_i) = E_i + \left(C_i + A_i \, r_i^{-\beta_i}\right) D_i^{-\alpha_{D,i}}}$$

- $E_i$：不可约误差（含 $a^*$、泛化误差基线等与 $r_i$ 无关的项）
- $C_i \, D_i^{-\alpha_{D,i}}$：数据受限损失——由未学习 quanta 贡献，仅依赖唯一数据 $D_i$
- $A_i \, r_i^{-\beta_i} \, D_i^{-\alpha_{D,i}}$：优化噪声损失——由已学习 quanta 的不完美学习贡献，被重复压制

**极限行为**：
- $r_i = 1$（单 epoch）：$L_i = E_i + (C_i + A_i) D_i^{-\alpha_{D,i}}$，噪声与数据限制共同主导
- $r_i \to \infty$（完美收敛）：$L_i = E_i + C_i \, D_i^{-\alpha_{D,i}}$，纯信息受限

**与 Dai & Zheng (2026) 的联系**：他们的 Extended Linear Model 将 loss 分解为 Capacity Term $c_i x_i^{-b_i}$ 和 Noise Term $A_i (D h_i)^{-a_i}$。上述公式中 $C_i D_i^{-\alpha_D}$ 对应 capacity term（对 quanta 视角的改写），而 $A_i r_i^{-\beta} D_i^{-\alpha_D}$ 对应 noise term——但修正了重复数据与独立数据的区别：独立数据的 noise term 为 $(r_i D_i)^{-a_i}$，重复数据则分离为 $r_i^{-\beta}$ 和 $D_i^{-\alpha_D}$ 两个独立因子，其中 $\beta \leq a_i$ 反映重复数据的信息衰减。

---

## 五、多域最优重复

### 5.1 优化问题

给定各域唯一数据 $D_i$、目标分布 $w_i$、总 token 预算 $T$：

$$\min_{\{r_i \geq 1\}} \mathcal{J} = \sum_{i=1}^K w_i L_i(r_i) \quad \text{s.t.} \quad \sum_{i=1}^K r_i D_i = T$$

由于 $E_i$ 和 $C_i D_i^{-\alpha_{D,i}}$ 均不含 $r_i$，问题简化为

$$\min_{\{r_i \geq 1\}} \sum_{i=1}^K \tilde{w}_i \, r_i^{-\beta_i} \quad \text{s.t.} \quad \sum_{i=1}^K r_i D_i = T$$

其中 $\tilde{w}_i = w_i A_i D_i^{-\alpha_{D,i}}$ 是域 $i$ 的噪声压制的"边际价值"。该目标函数为凸，存在唯一全局最优。

### 5.2 求解（Lagrange 乘数法）

Lagrangian：$\mathcal{L} = \sum_i \tilde{w}_i r_i^{-\beta_i} + \mu \left(\sum_i r_i D_i - T\right)$

一阶条件 $\partial \mathcal{L} / \partial r_i = 0$：

$$-\tilde{w}_i \beta_i \, r_i^{-(\beta_i + 1)} + \mu D_i = 0$$

$$\boxed{r_i^* = \left(\frac{\tilde{w}_i \, \beta_i}{\mu \, D_i}\right)^{1/(\beta_i + 1)} = \left(\frac{w_i A_i \beta_i \, D_i^{-\alpha_{D,i}}}{\mu \, D_i}\right)^{1/(\beta_i + 1)}}$$

其中 $\mu > 0$ 由预算约束 $\sum_i r_i^* D_i = T$ 隐式确定。

### 5.3 齐次情形（$\beta_i = \beta$）

若各域共享同一噪声衰减指数 $\beta$：

$$r_i^* \propto \left(\frac{w_i A_i}{D_i^{1 + \alpha_{D,i}}}\right)^{1/(\beta + 1)}$$

**直觉**：
- **目标权重 $w_i$ 越大的域，重复越多**——因为目标更关注该域的表现
- **噪声幅度 $A_i$ 越大的域，重复越多**——该域的优化噪声边际收益更高
- **唯一数据 $D_i$ 越多的域，重复越少**——已有足够样本，噪声本身较低

最优 token 分配：

$$T_i^* = r_i^* D_i \propto D_i^{(\beta - \alpha_{D,i})/(\beta+1)} \, (w_i A_i)^{1/(\beta+1)}$$

当 $\beta > \alpha_{D,i}$（通常成立）时，$T_i^*$ 随 $D_i$ 递增但亚线性——大域获得更多 token，但增速减缓。

---

## 六、过拟合修正与有限最优 $r^*$

上述公式中 $A_i r_i^{-\beta}$ 单调递减，暗示 $r_i \to \infty$ 最优——这忽略了过拟合。当 $r_i$ 过大，模型记忆训练样本而非学习抽象 quanta，测试损失反升。

在 per-quantum 层面，过拟合使 quantum $k$ 的测试损失增加：

$$a_k^{(\text{test})}(r_i) = \tilde{a}_k + \frac{g}{(r_i D_i p_k)^\beta} + \eta \left(\frac{r_i}{D_i p_k}\right)^\delta$$

第三项随 $r_i / (D_i p_k)$ 增长——每个唯一样本被重复的次数越多，过拟合越严重。聚合后（当 $\delta > \alpha_{D,i}$，高频 quantum 过拟合更快）：

$$\boxed{L_i(D_i, r_i) = E_i + \left(C_i + A_i \, r_i^{-\beta_i} + B_i \, r_i^{\delta_i}\right) D_i^{-\alpha_{D,i}}}$$

括号内的系数 $f(r) = C + Ar^{-\beta} + Br^{\delta}$ 呈 U 型曲线。单域最优重复：

$$\frac{\partial f}{\partial r} = 0 \implies r_i^{\text{opt}} = \left(\frac{\beta_i A_i}{\delta_i B_i}\right)^{1/(\beta_i + \delta_i)}$$

**$r_i^{\text{opt}}$ 的含义**：
- $A_i / B_i$ 大（噪声强、不易过拟合）→ 多重复
- $\beta_i / \delta_i$ 大（噪声衰减快、过拟合慢）→ 多重复
- 该最优值与 $D_i$ 无关（在 $\delta > \alpha_D$ 的假设下），仅取决于域的"可学习性"参数

---

## 七、与容量竞争的交互

Dai & Zheng 的容量竞争模型中，各域共享模型容量 $N$。在数据受限场景下，域 $i$ 最多能利用 $n_i \propto D_i^{1/(\alpha_i+1)}$ 单位容量（受唯一数据限制）。这产生额外约束：

$$x_i \leq n_i(D_i) \quad \forall i$$

若某域数据极少（$n_i \ll x_i^*$，其中 $x_i^*$ 为无数据约束下的最优容量分配），该域的"浪费容量" $x_i^* - n_i$ 可被重新分配给其他域——这使得数据丰富的域间接受益于数据稀缺域的存在。

**重复的间接效应**：增大 $r_i$ 提高 $h_i = r_i D_i / T$，改变容量分配。但由于 $n_i$ 不变，多给域 $i$ 的容量在超过 $n_i$ 后无法被利用。因此，数据受限域的最优策略是：重复到 $r_i^{\text{opt}}$（压制噪声至过拟合平衡点），而非无限增大 $h_i$。

---

## 八、可验证预测与实践建议

### 可验证预测

1. **$L$ vs $r$ 曲线**：固定 $D$，改变 $r$，损失应呈 $E + (C + Ar^{-\beta})D^{-\alpha_D}$ 形式。对 $L$ 做 $D^{\alpha_D}$ rescaling 后，各 $D$ 的曲线应坍缩到同一条 $f(r) = C + Ar^{-\beta}$ 上。
2. **重复 vs 独立数据**：相同总 token 数 $T$，重复 $r$ 次 $D$ 唯一数据应劣于 $T$ 个独立数据。差距大小量化了 $\beta$ 与 fresh data 指数 $a_i$ 的偏离。
3. **跨域最优 $r_i$ 的异质性**：不同域的最优重复次数应因 $A_i$、$B_i$、$\beta_i$、$\delta_i$ 的不同而显著不同——"难学域"（$A_i$ 大）需要更多重复。

### 参数估计

实际应用中，$C_i$、$A_i$、$\beta_i$（以及 $B_i$、$\delta_i$）可从少量 pilot 实验中拟合：

- 取 3-5 个不同的 $r$ 值（如 1, 2, 4, 8, 16 epoch），在小模型上训练
- 用最小二乘法拟合 $f(r) = C + Ar^{-\beta} + Br^{\delta}$（或忽略过拟合项）
- 外推得到最优 $r_i^*$，并通过预算约束分配 token

这一流程与 Dai & Zheng 提出的 proxy model fitting 流程兼容——区别在于本框架仅需 $O(K)$ 个参数（每域 3-5 个），而非 $O(K^2)$。

### 核心公式汇总

| 量 | 公式 | 含义 |
|---|---|---|
| 可学习 quanta 数 | $n_i \propto D_i^{1/(\alpha_i+1)}$ | 仅取决于唯一数据 |
| 域损失 | $L_i = E_i + (C_i + A_i r_i^{-\beta_i}) D_i^{-\alpha_{D,i}}$ | 噪声被重复压制 |
| 含过拟合 | $L_i = E_i + (C_i + A_i r_i^{-\beta_i} + B_i r_i^{\delta_i}) D_i^{-\alpha_{D,i}}$ | U 型曲线 |
| 单域最优重复 | $r_i^{\text{opt}} = (\beta_i A_i / \delta_i B_i)^{1/(\beta_i+\delta_i)}$ | 噪声-过拟合平衡 |
| 多域最优重复 | $r_i^* = (\tilde{w}_i \beta_i / \mu D_i)^{1/(\beta_i+1)}$ | 带预算约束 |
