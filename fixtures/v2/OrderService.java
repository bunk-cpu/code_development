package demo.order;

public class OrderService {
    public String summary(String tenantId, String status) {
        AuditClient.recordRead(tenantId); // 示例缺依赖：只能标为未解析
        return "tenant=" + tenantId + ";status=" + status;
    }
}
