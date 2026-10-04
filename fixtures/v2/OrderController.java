package demo.order;

// 示例源码，不属于真实客户项目。
public class OrderController {
    private final OrderService service = new OrderService();
    public String summary(String tenantId, String status) {
        OrderPolicy.requireRead(tenantId);
        if (!status.equals("all") && !status.equals("pending") && !status.equals("completed"))
            throw new IllegalArgumentException("invalid status");
        return service.summary(tenantId, status);
    }
}
