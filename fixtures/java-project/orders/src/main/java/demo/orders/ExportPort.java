package demo.orders;
import java.util.List;
public interface ExportPort {
    List<String> create(String tenantId, int limit);
}
